import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from rich.table import Table
from rich.panel import Panel

from logging_config import console

logger = logging.getLogger("ergo_arb.tracker")

SCHEMA_VERSION = 3


class ProfitTracker:
    def __init__(self, db_path: str = "arbitrage_tracker.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # WAL lets a dashboard read while the scanner writes
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._migrate()
        self._session_start = datetime.now()
        # Episodes: one row per continuous run of a profitable path
        self._open_episodes: dict[str, int] = {}  # path_key -> episode id
        self._session_episode_best: dict[int, float] = {}  # episode id -> best profit
        self._close_stale_episodes()
        # Episodes a stopped process left open: kept so the scanner can close their Discord message
        self.stale_chain_episodes = [dict(r) for r in self.conn.execute(
            "SELECT * FROM chain_episodes WHERE closed_at IS NULL").fetchall()]
        self.conn.execute("UPDATE chain_episodes SET closed_at = COALESCE(last_seen_at, opened_at) "
                          "WHERE closed_at IS NULL")
        self.conn.commit()
        for row in self.stale_chain_episodes:
            row["closed_at"] = row.get("last_seen_at") or row["opened_at"]

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                nonkyc_erg_usdt_bid REAL,
                nonkyc_erg_usdt_ask REAL,
                kucoin_erg_usdt_bid REAL,
                kucoin_erg_usdt_ask REAL,
                spectrum_erg_sigusd REAL,
                oracle_erg_usd REAL,
                bank_reserve_ratio REAL,
                bank_can_mint INTEGER,
                bank_can_redeem INTEGER
            );

            CREATE TABLE IF NOT EXISTS scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                scan_number INTEGER NOT NULL,
                snapshot_id INTEGER,
                path TEXT NOT NULL,
                input_erg REAL NOT NULL,
                output_erg REAL NOT NULL,
                profit_erg REAL NOT NULL,
                profit_percent REAL NOT NULL,
                is_profitable INTEGER NOT NULL DEFAULT 0,
                source_exchange TEXT,
                target_exchange TEXT,
                source_price REAL,
                target_price REAL,
                fee_total_erg REAL DEFAULT 0,
                fee_total_usd REAL DEFAULT 0,
                FOREIGN KEY (snapshot_id) REFERENCES price_snapshots(id)
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                scan_number INTEGER,
                path TEXT NOT NULL,
                input_erg REAL NOT NULL,
                output_erg REAL NOT NULL,
                profit_erg REAL NOT NULL,
                profit_percent REAL NOT NULL,
                source_exchange TEXT,
                target_exchange TEXT,
                source_price REAL,
                target_price REAL,
                fee_trading REAL DEFAULT 0,
                fee_withdraw_erg REAL DEFAULT 0,
                fee_network_erg REAL DEFAULT 0,
                fee_protocol REAL DEFAULT 0,
                fee_execution_erg REAL DEFAULT 0,
                fee_slippage REAL DEFAULT 0,
                fee_total_erg REAL DEFAULT 0,
                fee_total_usd REAL DEFAULT 0,
                snapshot_id INTEGER,
                executed INTEGER DEFAULT 0,
                FOREIGN KEY (snapshot_id) REFERENCES price_snapshots(id)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_seconds REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                input_erg REAL NOT NULL,
                expected_output_erg REAL,
                actual_output_erg REAL,
                expected_profit_erg REAL,
                actual_profit_erg REAL,
                fee_paid_erg REAL,
                fee_paid_usd REAL,
                tx_ids TEXT,
                error_message TEXT,
                notes TEXT,
                FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
            );

            CREATE TABLE IF NOT EXISTS daily_summary (
                date TEXT PRIMARY KEY,
                total_scans INTEGER DEFAULT 0,
                opportunities_found INTEGER DEFAULT 0,
                trades_attempted INTEGER DEFAULT 0,
                trades_completed INTEGER DEFAULT 0,
                trades_failed INTEGER DEFAULT 0,
                total_profit_erg REAL DEFAULT 0,
                total_fees_erg REAL DEFAULT 0,
                best_profit_erg REAL DEFAULT 0,
                worst_loss_erg REAL DEFAULT 0,
                avg_spread_percent REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS opportunity_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                closed_at TEXT,
                scans INTEGER NOT NULL DEFAULT 1,
                first_scan_number INTEGER,
                best_profit_erg REAL NOT NULL,
                best_profit_percent REAL NOT NULL,
                best_size_erg REAL NOT NULL,
                opportunity_id INTEGER,
                FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
            );
        """)
        self.conn.commit()

    def _migrate(self):
        """Schema versioning via PRAGMA user_version; each step is idempotent."""
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self.conn.executescript("""
                CREATE INDEX IF NOT EXISTS ix_scan_results_ts ON scan_results(timestamp);
                CREATE INDEX IF NOT EXISTS ix_scan_results_path ON scan_results(path);
                CREATE INDEX IF NOT EXISTS ix_opportunities_ts ON opportunities(timestamp);
                CREATE INDEX IF NOT EXISTS ix_episodes_path ON opportunity_episodes(path, first_seen);
            """)
        if version < 2:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS chain_episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    peak_profit_erg REAL NOT NULL,
                    peak_profit_percent REAL NOT NULL,
                    peak_size_erg REAL NOT NULL,
                    last_profit_percent REAL,
                    trade TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_chain_episodes_opened ON chain_episodes(opened_at);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)
        if version < 3:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(chain_episodes)")}
            if "message_id" not in cols:
                self.conn.execute("ALTER TABLE chain_episodes ADD COLUMN message_id TEXT")
            if "last_seen_at" not in cols:
                self.conn.execute("ALTER TABLE chain_episodes ADD COLUMN last_seen_at TEXT")
        if version < SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()

    def _close_stale_episodes(self):
        """Close episodes left open by a previous process (crash or kill)."""
        self.conn.execute(
            "UPDATE opportunity_episodes SET closed_at = last_seen WHERE closed_at IS NULL"
        )
        self.conn.commit()

    def record_scan(self, opportunities: list, scan_number: int, snapshot_id: int = None):
        """Open, extend or close opportunity episodes for this scan.

        A path that stays profitable for many scans is one episode; its potential
        profit is counted once, at the best size seen during the episode.
        """
        now = datetime.now().isoformat()
        best: dict = {}
        for opp in opportunities:
            if not opp.is_profitable or opp.blocked:
                continue
            key = opp.path_key
            if key not in best or opp.profit_erg > best[key].profit_erg:
                best[key] = opp

        for key, opp in best.items():
            episode_id = self._open_episodes.get(key)
            if episode_id is None:
                opp_id = self.log_opportunity(opp, scan_number=scan_number, snapshot_id=snapshot_id)
                cursor = self.conn.execute(
                    """INSERT INTO opportunity_episodes
                       (path, first_seen, last_seen, scans, first_scan_number,
                        best_profit_erg, best_profit_percent, best_size_erg, opportunity_id)
                       VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                    (key, now, now, scan_number, opp.profit_erg, opp.profit_percent, opp.input_erg, opp_id),
                )
                self._open_episodes[key] = cursor.lastrowid
                self._session_episode_best[cursor.lastrowid] = opp.profit_erg
                continue
            self.conn.execute(
                "UPDATE opportunity_episodes SET last_seen = ?, scans = scans + 1 WHERE id = ?",
                (now, episode_id),
            )
            if opp.profit_erg > self._session_episode_best[episode_id]:
                self.conn.execute(
                    """UPDATE opportunity_episodes
                       SET best_profit_erg = ?, best_profit_percent = ?, best_size_erg = ?
                       WHERE id = ?""",
                    (opp.profit_erg, opp.profit_percent, opp.input_erg, episode_id),
                )
                self._session_episode_best[episode_id] = opp.profit_erg

        for key in [k for k in self._open_episodes if k not in best]:
            episode_id = self._open_episodes.pop(key)
            self.conn.execute(
                "UPDATE opportunity_episodes SET closed_at = ? WHERE id = ?", (now, episode_id)
            )
            logger.info(f"Episode #{episode_id} closed: {key}")
        self.conn.commit()

    def prune_scan_results(self, days: int) -> int:
        """Delete non-profitable scan_results older than `days`. Returns rows deleted."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM scan_results WHERE is_profitable = 0 AND timestamp < ?", (cutoff,)
        )
        self.conn.commit()
        return cursor.rowcount

    def log_price_snapshot(self, prices: dict) -> int:
        """Log a price snapshot from all sources."""
        bank = prices.get("bank", {})
        ob = prices.get("nonkyc_orderbook")
        n_bid = ob.best_bid.price if ob and ob.best_bid else None
        n_ask = ob.best_ask.price if ob and ob.best_ask else None
        kob = prices.get("kucoin_orderbook")
        k_bid = kob.best_bid.price if kob and kob.best_bid else None
        k_ask = kob.best_ask.price if kob and kob.best_ask else None

        cursor = self.conn.execute(
            """INSERT INTO price_snapshots
               (timestamp, nonkyc_erg_usdt_bid, nonkyc_erg_usdt_ask,
                kucoin_erg_usdt_bid, kucoin_erg_usdt_ask,
                spectrum_erg_sigusd, oracle_erg_usd, bank_reserve_ratio,
                bank_can_mint, bank_can_redeem)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                n_bid, n_ask, k_bid, k_ask,
                prices.get("spectrum_erg_sigusd"),
                bank.get("oracle_erg_usd"),
                bank.get("reserve_ratio"),
                1 if bank.get("can_mint_sigusd") else 0,
                1 if bank.get("can_redeem_sigusd") else 0,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def log_scan_results(self, opportunities: list, scan_number: int, snapshot_id: int = None):
        """Log ALL scan results (every opportunity analyzed, profitable or not)."""
        now = datetime.now().isoformat()
        for opp in opportunities:
            self.conn.execute(
                """INSERT INTO scan_results
                   (timestamp, scan_number, snapshot_id, path, input_erg, output_erg,
                    profit_erg, profit_percent, is_profitable,
                    source_exchange, target_exchange, source_price, target_price,
                    fee_total_erg, fee_total_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now, scan_number, snapshot_id,
                    opp.path, opp.input_erg, opp.output_erg,
                    opp.profit_erg, opp.profit_percent,
                    1 if opp.is_profitable else 0,
                    opp.source_exchange, opp.target_exchange,
                    opp.source_price, opp.target_price,
                    opp.fees.total_fee_erg, opp.fees.total_fee_usd,
                ),
            )
        self.conn.commit()
        logger.debug(f"Logged {len(opportunities)} scan results for scan #{scan_number}")

    def log_opportunity(self, opp, scan_number: int = 0, snapshot_id: int = None) -> int:
        """Log a profitable opportunity with full fee breakdown (once per episode)."""
        cursor = self.conn.execute(
            """INSERT INTO opportunities
               (timestamp, scan_number, path, input_erg, output_erg, profit_erg, profit_percent,
                source_exchange, target_exchange, source_price, target_price,
                fee_trading, fee_withdraw_erg, fee_network_erg, fee_protocol,
                fee_execution_erg, fee_slippage, fee_total_erg, fee_total_usd, snapshot_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opp.timestamp.isoformat(),
                scan_number,
                opp.path,
                opp.input_erg,
                opp.output_erg,
                opp.profit_erg,
                opp.profit_percent,
                opp.source_exchange,
                opp.target_exchange,
                opp.source_price,
                opp.target_price,
                opp.fees.trading_fee,
                opp.fees.withdraw_fee_erg,
                opp.fees.network_fee_erg,
                opp.fees.protocol_fee,
                opp.fees.execution_fee_erg,
                opp.fees.slippage_cost,
                opp.fees.total_fee_erg,
                opp.fees.total_fee_usd,
                snapshot_id,
            ),
        )
        self.conn.commit()
        opp_id = cursor.lastrowid
        logger.info(
            f"Opportunity #{opp_id} logged: {opp.path} | "
            f"+{opp.profit_erg:.4f} ERG ({opp.profit_percent:+.2f}%) | "
            f"Fees: {opp.fees.total_fee_erg:.4f} ERG + ${opp.fees.total_fee_usd:.4f}"
        )
        return opp_id

    def start_trade(self, opportunity_id: int, input_erg: float,
                    expected_output: float, expected_profit: float) -> int:
        """Log the start of a trade execution."""
        cursor = self.conn.execute(
            """INSERT INTO trades
               (opportunity_id, started_at, status, input_erg,
                expected_output_erg, expected_profit_erg)
               VALUES (?, ?, 'executing', ?, ?, ?)""",
            (opportunity_id, datetime.now().isoformat(), input_erg,
             expected_output, expected_profit),
        )
        self.conn.execute(
            "UPDATE opportunities SET executed = 1 WHERE id = ?",
            (opportunity_id,),
        )
        self.conn.commit()
        trade_id = cursor.lastrowid
        logger.info(f"Trade #{trade_id} started for opportunity #{opportunity_id}")
        return trade_id

    def set_trade_input(self, trade_id: int, input_erg: float, expected_output: float, expected_profit: float):
        """Record the size and plan the trade actually went out with (it is sized at execution time)."""
        self.conn.execute(
            "UPDATE trades SET input_erg = ?, expected_output_erg = ?, expected_profit_erg = ? WHERE id = ?",
            (input_erg, expected_output, expected_profit, trade_id),
        )
        self.conn.commit()

    def complete_trade(self, trade_id: int, actual_output: float,
                       fee_paid_erg: float = 0, fee_paid_usd: float = 0,
                       tx_ids: list[str] = None, notes: str = ""):
        """Log successful trade completion."""
        now = datetime.now().isoformat()
        row = self.conn.execute(
            "SELECT started_at, input_erg FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()

        duration = None
        actual_profit = None
        if row:
            started = datetime.fromisoformat(row["started_at"])
            duration = (datetime.now() - started).total_seconds()
            actual_profit = actual_output - row["input_erg"]

        self.conn.execute(
            """UPDATE trades SET
               completed_at = ?, duration_seconds = ?, status = 'completed',
               actual_output_erg = ?, actual_profit_erg = ?,
               fee_paid_erg = ?, fee_paid_usd = ?,
               tx_ids = ?, notes = ?
               WHERE id = ?""",
            (now, duration, actual_output, actual_profit,
             fee_paid_erg, fee_paid_usd,
             json.dumps(tx_ids or []), notes, trade_id),
        )
        self.conn.commit()

        if actual_profit is not None:
            status = "PROFIT" if actual_profit > 0 else "LOSS"
            logger.info(
                f"Trade #{trade_id} completed: {status} {actual_profit:+.4f} ERG "
                f"in {duration:.1f}s | Fees: {fee_paid_erg:.4f} ERG + ${fee_paid_usd:.4f}"
            )

    def fail_trade(self, trade_id: int, error: str, notes: str = ""):
        """Log a failed trade."""
        now = datetime.now().isoformat()
        row = self.conn.execute(
            "SELECT started_at FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()

        duration = None
        if row:
            started = datetime.fromisoformat(row["started_at"])
            duration = (datetime.now() - started).total_seconds()

        self.conn.execute(
            """UPDATE trades SET
               completed_at = ?, duration_seconds = ?, status = 'failed',
               error_message = ?, notes = ?
               WHERE id = ?""",
            (now, duration, error, notes, trade_id),
        )
        self.conn.commit()
        logger.error(f"Trade #{trade_id} failed after {duration:.1f}s: {error}")

    def update_daily_summary(self, opportunities: int):
        """Count one scan in today's summary (accumulates across restarts)."""
        today = datetime.now().strftime("%Y-%m-%d")

        # Get today's trade stats
        trade_stats = self.conn.execute("""
            SELECT
                COUNT(*) as attempted,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                COALESCE(SUM(CASE WHEN status = 'completed' THEN actual_profit_erg ELSE 0 END), 0) as profit,
                COALESCE(SUM(CASE WHEN status = 'completed' THEN fee_paid_erg ELSE 0 END), 0) as fees,
                COALESCE(MAX(CASE WHEN status = 'completed' THEN actual_profit_erg END), 0) as best,
                COALESCE(MIN(CASE WHEN status = 'completed' THEN actual_profit_erg END), 0) as worst
            FROM trades
            WHERE DATE(started_at) = ?
        """, (today,)).fetchone()

        self.conn.execute("""
            INSERT INTO daily_summary (date, total_scans, opportunities_found,
                trades_attempted, trades_completed, trades_failed,
                total_profit_erg, total_fees_erg, best_profit_erg, worst_loss_erg)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_scans = total_scans + 1,
                opportunities_found = opportunities_found + ?,
                trades_attempted = ?,
                trades_completed = ?,
                trades_failed = ?,
                total_profit_erg = ?,
                total_fees_erg = ?,
                best_profit_erg = ?,
                worst_loss_erg = ?
        """, (
            today, opportunities,
            trade_stats["attempted"], trade_stats["completed"], trade_stats["failed"],
            trade_stats["profit"], trade_stats["fees"],
            trade_stats["best"], trade_stats["worst"],
            # ON CONFLICT values
            opportunities,
            trade_stats["attempted"], trade_stats["completed"], trade_stats["failed"],
            trade_stats["profit"], trade_stats["fees"],
            trade_stats["best"], trade_stats["worst"],
        ))
        self.conn.commit()

    def get_session_stats(self) -> dict:
        return {
            "opportunities_seen": len(self._session_episode_best),
            "total_potential_profit_erg": sum(self._session_episode_best.values()),
            "session_duration": str(datetime.now() - self._session_start).split(".")[0],
        }

    def get_historical_stats(self) -> dict:
        row = self.conn.execute("""
            SELECT COUNT(*), SUM(profit_erg), AVG(profit_percent),
                   MAX(profit_erg), MIN(profit_erg),
                   AVG(fee_total_erg)
            FROM opportunities
        """).fetchone()
        trade_row = self.conn.execute("""
            SELECT COUNT(*), SUM(actual_profit_erg), SUM(fee_paid_erg),
                   AVG(duration_seconds)
            FROM trades WHERE status='completed'
        """).fetchone()
        return {
            "total_opportunities": row[0] or 0,
            "total_potential_profit": row[1] or 0.0,
            "avg_profit_percent": row[2] or 0.0,
            "best_opportunity_erg": row[3] or 0.0,
            "worst_opportunity_erg": row[4] or 0.0,
            "avg_fees_erg": row[5] or 0.0,
            "trades_executed": trade_row[0] or 0,
            "actual_profit_erg": trade_row[1] or 0.0,
            "total_fees_paid_erg": trade_row[2] or 0.0,
            "avg_trade_duration_s": trade_row[3] or 0.0,
        }

    def get_recent_opportunities(self, limit: int = 10) -> list[dict]:
        """Get most recent opportunities for review."""
        rows = self.conn.execute("""
            SELECT * FROM opportunities
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_fee_analysis(self) -> dict:
        """Analyze fee impact across all opportunities."""
        row = self.conn.execute("""
            SELECT
                AVG(fee_trading) as avg_trading_fee,
                AVG(fee_withdraw_erg) as avg_withdraw_fee,
                AVG(fee_network_erg) as avg_network_fee,
                AVG(fee_protocol) as avg_protocol_fee,
                AVG(fee_execution_erg) as avg_execution_fee,
                AVG(fee_slippage) as avg_slippage_cost,
                AVG(fee_total_erg) as avg_total_fee_erg,
                SUM(fee_total_erg) as sum_total_fee_erg
            FROM opportunities
        """).fetchone()
        if row and row[0] is not None:
            return dict(row)
        return {}

    def get_opportunity_frequency(self, hours: int = 24) -> dict:
        """Get stats on opportunity frequency over the last N hours."""
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total,
                AVG(profit_percent) as avg_profit_pct,
                MAX(profit_percent) as best_profit_pct,
                MAX(profit_erg) as best_profit_erg,
                AVG(profit_erg) as avg_profit_erg
            FROM opportunities
            WHERE timestamp >= datetime('now', ?)
        """, (f"-{hours} hours",)).fetchone()

        # Most common profitable path
        path_row = self.conn.execute("""
            SELECT path, COUNT(*) as cnt
            FROM opportunities
            WHERE timestamp >= datetime('now', ?)
            GROUP BY path
            ORDER BY cnt DESC
            LIMIT 1
        """, (f"-{hours} hours",)).fetchone()

        # Best single opportunity
        best_row = self.conn.execute("""
            SELECT path, profit_erg, profit_percent, input_erg
            FROM opportunities
            WHERE timestamp >= datetime('now', ?)
            ORDER BY profit_percent DESC
            LIMIT 1
        """, (f"-{hours} hours",)).fetchone()

        result = {
            "hours": hours,
            "total_opportunities": row[0] if row else 0,
            "avg_profit_percent": row[1] if row and row[1] else 0.0,
            "best_profit_percent": row[2] if row and row[2] else 0.0,
            "best_profit_erg": row[3] if row and row[3] else 0.0,
            "avg_profit_erg": row[4] if row and row[4] else 0.0,
            "most_common_path": path_row[0] if path_row else "N/A",
            "most_common_path_count": path_row[1] if path_row else 0,
        }

        if best_row:
            result["best_opportunity"] = {
                "path": best_row[0],
                "profit_erg": best_row[1],
                "profit_percent": best_row[2],
                "input_erg": best_row[3],
            }
        else:
            result["best_opportunity"] = None

        return result

    def get_opportunity_history(
        self, path_filter: str = None, limit: int = 50
    ) -> list[dict]:
        """Get historical opportunities, optionally filtered by path."""
        if path_filter:
            rows = self.conn.execute("""
                SELECT * FROM opportunities
                WHERE path LIKE ?
                ORDER BY id DESC LIMIT ?
            """, (f"%{path_filter}%", limit)).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT * FROM opportunities
                ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def print_summary(self):
        """Print comprehensive session summary."""
        session = self.get_session_stats()
        historical = self.get_historical_stats()
        fee_analysis = self.get_fee_analysis()

        # Main stats table
        table = Table(title="Session Summary", show_header=True, header_style="bold magenta")
        table.add_column("Metric")
        table.add_column("This Session", justify="right")
        table.add_column("All Time", justify="right")

        table.add_row("Duration", session["session_duration"], "-")
        table.add_row(
            "Opportunities Found",
            str(session["opportunities_seen"]),
            str(historical["total_opportunities"]),
        )
        table.add_row(
            "Potential Profit (ERG)",
            f"{session['total_potential_profit_erg']:.4f}",
            f"{historical['total_potential_profit']:.4f}",
        )
        table.add_row("Avg Profit %", "-", f"{historical['avg_profit_percent']:.2f}%")
        table.add_row("Best Opp (ERG)", "-", f"+{historical['best_opportunity_erg']:.4f}")
        table.add_row("Trades Executed", "-", str(historical["trades_executed"]))
        table.add_row("Actual Profit (ERG)", "-", f"{historical['actual_profit_erg']:.4f}")
        table.add_row("Total Fees Paid (ERG)", "-", f"{historical['total_fees_paid_erg']:.4f}")
        if historical["avg_trade_duration_s"] > 0:
            table.add_row("Avg Trade Time", "-", f"{historical['avg_trade_duration_s']:.1f}s")

        console.print(table)

        # Fee breakdown table
        if fee_analysis:
            fee_table = Table(title="Fee Analysis (averages)", show_header=True, header_style="bold cyan")
            fee_table.add_column("Fee Type")
            fee_table.add_column("Avg Per Trade", justify="right")

            fee_table.add_row("Trading Fee (USD)", f"${fee_analysis.get('avg_trading_fee', 0):.4f}")
            fee_table.add_row("Withdrawal Fee (ERG)", f"{fee_analysis.get('avg_withdraw_fee', 0):.4f}")
            fee_table.add_row("Network Fee (ERG)", f"{fee_analysis.get('avg_network_fee', 0):.4f}")
            fee_table.add_row("Protocol Fee (USD)", f"${fee_analysis.get('avg_protocol_fee', 0):.4f}")
            fee_table.add_row("Execution Fee (ERG)", f"{fee_analysis.get('avg_execution_fee', 0):.4f}")
            fee_table.add_row("Slippage Cost (ERG)", f"{fee_analysis.get('avg_slippage_cost', 0):.4f}")
            fee_table.add_row("Total Fees (ERG)", f"{fee_analysis.get('avg_total_fee_erg', 0):.4f}")

            console.print(fee_table)

    def open_chain_episode(self, ep) -> int:
        cur = self.conn.execute(
            """INSERT INTO chain_episodes (path, opened_at, peak_profit_erg, peak_profit_percent, peak_size_erg,
               last_profit_percent) VALUES (?, ?, ?, ?, ?, ?)""",
            (ep.label, datetime.now().isoformat(), ep.peak_erg, ep.peak_percent, ep.peak_size_erg, ep.profit_percent))
        self.conn.commit()
        return cur.lastrowid

    def update_chain_episode(self, episode_id: int, ep):
        self.conn.execute(
            """UPDATE chain_episodes SET peak_profit_erg = ?, peak_profit_percent = ?, peak_size_erg = ?,
               last_profit_percent = ?, last_seen_at = ? WHERE id = ?""",
            (ep.peak_erg, ep.peak_percent, ep.peak_size_erg, ep.profit_percent, datetime.now().isoformat(),
             episode_id))
        self.conn.commit()

    def set_chain_episode_message(self, episode_id: int, message_id: str):
        self.conn.execute("UPDATE chain_episodes SET message_id = ? WHERE id = ?", (message_id, episode_id))
        self.conn.commit()

    def close_chain_episode(self, episode_id: int, ep):
        self.update_chain_episode(episode_id, ep)
        self.conn.execute("UPDATE chain_episodes SET closed_at = ?, trade = ? WHERE id = ?",
                          (datetime.now().isoformat(), ep.trade, episode_id))
        self.conn.commit()

    def chain_episodes_since(self, since_iso: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM chain_episodes WHERE opened_at >= ? ORDER BY opened_at",
                                 (since_iso,)).fetchall()
        return [dict(r) for r in rows]

    def trades_since(self, since_iso: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trades WHERE started_at >= ? ORDER BY started_at",
                                 (since_iso,)).fetchall()
        return [dict(r) for r in rows]

    def get_meta(self, key: str):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()

    def close(self):
        self.conn.close()
