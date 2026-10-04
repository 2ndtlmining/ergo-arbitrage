import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from rich.table import Table

from logging_config import console

logger = logging.getLogger("ergo_arb.tracker")

SCHEMA_VERSION = 4


class ProfitTracker:
    def __init__(self, db_path: str = "arbitrage_tracker.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, timeout=10)  # another process (backup, arb.py) may hold a write briefly
        self.conn.row_factory = sqlite3.Row
        self._batch_depth = 0
        # new databases give freed pages back to the file system (incremental_vacuum after a prune);
        # must be set before the first table exists, existing databases keep reusing freed pages
        if not self.conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone():
            self.conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        # WAL lets a dashboard read while the scanner writes
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._migrate()
        self._session_start = datetime.now()
        # Episodes: one row per continuous run of a profitable path
        self._open_episodes: dict[str, int] = {}     # path_key -> index into _session_episode_best
        self._session_episode_best: list[float] = []  # best profit of each run of a profitable path
        self._close_stale_episodes()

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
        self._commit()

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
        if version < 4:
            # "Spectrum" (sunset) is "ErgoDEX" in path keys and venue names: rename stored history once
            for table, columns in (("scan_results", ("path", "source_exchange", "target_exchange")),
                                   ("opportunities", ("path", "source_exchange", "target_exchange")),
                                   ("opportunity_episodes", ("path",)), ("chain_episodes", ("path",))):
                for col in columns:
                    self.conn.execute(f"UPDATE {table} SET {col} = replace(replace({col}, 'Spectrum DEX', "
                                      f"'ErgoDEX pool'), 'Spectrum', 'ErgoDEX') WHERE {col} LIKE '%Spectrum%'")
        if version < SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._commit()

    def _close_stale_episodes(self):
        """Close episodes left open by a previous process (crash or kill)."""
        self.conn.execute(
            "UPDATE opportunity_episodes SET closed_at = last_seen WHERE closed_at IS NULL"
        )
        self._commit()

    def record_scan(self, opportunities: list, scan_number: int, snapshot_id: int = None):
        """One `opportunities` row (full fee breakdown) when a path turns profitable. The session counts
        each run of a profitable path once, at its best size. The on-chain episodes Discord reports are
        kept in chain_episodes; opportunity_episodes is no longer written (old rows stay)."""
        best: dict = {}
        for opp in opportunities:
            if not opp.is_profitable or opp.blocked:
                continue
            key = opp.path_key
            if key not in best or opp.profit_erg > best[key].profit_erg:
                best[key] = opp
        for key, opp in best.items():
            index = self._open_episodes.get(key)
            if index is None:
                self.log_opportunity(opp, scan_number=scan_number, snapshot_id=snapshot_id)
                self._open_episodes[key] = len(self._session_episode_best)
                self._session_episode_best.append(opp.profit_erg)
            elif opp.profit_erg > self._session_episode_best[index]:
                self._session_episode_best[index] = opp.profit_erg
        for key in [k for k in self._open_episodes if k not in best]:
            del self._open_episodes[key]
        self._commit()

    def _commit(self):
        if self._batch_depth == 0:
            self.conn.commit()

    @contextmanager
    def batch(self):
        """One commit for several writes (a scan's snapshot, results and episodes); rolled back on error."""
        self._batch_depth += 1
        try:
            yield
        except BaseException:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self.conn.rollback()
            raise
        self._batch_depth -= 1
        if self._batch_depth == 0:
            self.conn.commit()

    def prune(self, days: int, now: datetime = None) -> tuple[int, int]:
        """Delete non-profitable scan rows older than `days` and the old price snapshots nothing points to,
        then checkpoint the WAL and return freed pages. Returns (scan rows, snapshots) deleted."""
        cutoff = ((now or datetime.now()) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "DELETE FROM scan_results WHERE is_profitable = 0 AND timestamp < ?", (cutoff,)
        ).rowcount
        snaps = self.conn.execute(
            """DELETE FROM price_snapshots WHERE timestamp < ?
               AND id NOT IN (SELECT snapshot_id FROM scan_results WHERE snapshot_id IS NOT NULL)
               AND id NOT IN (SELECT snapshot_id FROM opportunities WHERE snapshot_id IS NOT NULL)""", (cutoff,)
        ).rowcount
        self.conn.commit()
        try:
            self.conn.execute("PRAGMA incremental_vacuum")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as e:  # another process reading: the next prune checkpoints instead
            logger.debug(f"checkpoint skipped: {e}")
        return rows, snaps

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
        self._commit()
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
        self._commit()
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
        self._commit()
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
        self._commit()
        trade_id = cursor.lastrowid
        logger.info(f"Trade #{trade_id} started for opportunity #{opportunity_id}")
        return trade_id

    def set_trade_input(self, trade_id: int, input_erg: float, expected_output: float, expected_profit: float):
        """Record the size and plan the trade actually went out with (it is sized at execution time)."""
        self.conn.execute(
            "UPDATE trades SET input_erg = ?, expected_output_erg = ?, expected_profit_erg = ? WHERE id = ?",
            (input_erg, expected_output, expected_profit, trade_id),
        )
        self._commit()

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
        self._commit()

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
        self._commit()
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
        self._commit()

    def get_session_stats(self) -> dict:
        return {
            "opportunities_seen": len(self._session_episode_best),
            "total_potential_profit_erg": sum(self._session_episode_best),
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
        self._commit()
        return cur.lastrowid

    def update_chain_episode(self, episode_id: int, ep):
        self.conn.execute(
            """UPDATE chain_episodes SET peak_profit_erg = ?, peak_profit_percent = ?, peak_size_erg = ?,
               last_profit_percent = ?, last_seen_at = ? WHERE id = ?""",
            (ep.peak_erg, ep.peak_percent, ep.peak_size_erg, ep.profit_percent, datetime.now().isoformat(),
             episode_id))
        self._commit()

    def set_chain_episode_message(self, episode_id: int, message_id: str):
        self.conn.execute("UPDATE chain_episodes SET message_id = ? WHERE id = ?", (message_id, episode_id))
        self._commit()

    def claim_stale_chain_episodes(self) -> list[dict]:
        """Close the episodes a stopped process left open and return them (for their Discord message).

        Only the long-running bot calls this at start; a --once run or another tool opening the same
        database must not close a running bot's episodes.
        """
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM chain_episodes WHERE closed_at IS NULL")]
        self.conn.execute("UPDATE chain_episodes SET closed_at = COALESCE(last_seen_at, opened_at) "
                          "WHERE closed_at IS NULL")
        self._commit()
        for row in rows:
            row["closed_at"] = row.get("last_seen_at") or row["opened_at"]
        return rows

    def touch_chain_episode(self, episode_id: int):
        """Heartbeat for an open episode, so a crash still records roughly how long it lasted."""
        self.conn.execute("UPDATE chain_episodes SET last_seen_at = ? WHERE id = ?",
                          (datetime.now().isoformat(), episode_id))
        self._commit()

    def close_chain_episode(self, episode_id: int, ep):
        self.update_chain_episode(episode_id, ep)
        self.conn.execute("UPDATE chain_episodes SET closed_at = ?, trade = ? WHERE id = ?",
                          (datetime.now().isoformat(), ep.trade, episode_id))
        self._commit()

    def chain_episodes_since(self, since_iso: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM chain_episodes WHERE opened_at >= ? ORDER BY opened_at",
                                 (since_iso,)).fetchall()
        return [dict(r) for r in rows]

    def recent_chain_episodes(self, limit: int = 5) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM chain_episodes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recent_trades(self, limit: int = 5) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
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
        self._commit()

    # --- live-mode state that must survive a restart (#59) ---

    def trades_started_on(self, day: str) -> list[dict]:
        """Trade attempts started on a local date (YYYY-MM-DD), oldest first."""
        rows = self.conn.execute("SELECT * FROM trades WHERE substr(started_at, 1, 10) = ? ORDER BY id",
                                 (day,)).fetchall()
        return [dict(r) for r in rows]

    def last_trade_started(self):
        """Start time (datetime) of the most recent trade attempt, or None."""
        row = self.conn.execute("SELECT started_at FROM trades ORDER BY id DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def unfinished_trades(self) -> list[dict]:
        """Trades still marked executing/pending: the bot stopped while they ran."""
        rows = self.conn.execute("SELECT * FROM trades WHERE status IN ('executing', 'pending') ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def delete_meta(self, key: str):
        self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        self._commit()

    def close(self):
        self.conn.close()
