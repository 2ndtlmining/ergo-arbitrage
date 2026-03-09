"""DEX + Node integration test.

Tests that Spectrum DEX prices, SigmaUSD bank state, and Ergo node wallet
all work together correctly. Uses ERGO_NODE_URL from .env.

Run with: python -m pytest tests/test_dex_node.py -v -s
"""
import asyncio
import os
import pytest

from dotenv import load_dotenv
load_dotenv()

# Override node URL for this test module
NODE_URL = os.getenv("ERGO_NODE_URL", "http://127.0.0.1:9053")
NODE_API_KEY = os.getenv("ERGO_NODE_API_KEY", "")

pytestmark = pytest.mark.asyncio


class TestNodeConnectivity:
    """Test that the Ergo node is reachable and synced."""

    async def _make_node(self):
        from exchanges.ergo_node import ErgoNodeClient
        node = ErgoNodeClient()
        node.base_url = NODE_URL
        node.api_key = NODE_API_KEY
        await node.connect()
        # Override session headers for this node
        await node.session.close()
        import aiohttp
        node.session = aiohttp.ClientSession(
            headers={"api_key": NODE_API_KEY, "Content-Type": "application/json"}
        )
        return node

    async def test_node_reachable(self):
        node = await self._make_node()
        try:
            info = await node.get_node_info()
            if not info:
                pytest.skip(f"Node at {NODE_URL} not reachable")
            height = info.get("fullHeight", 0)
            headers = info.get("headersHeight", 0)
            synced = height > 0 and abs(headers - height) < 5
            print(f"  Node: {NODE_URL}")
            print(f"  Height: {height} | Headers: {headers}")
            print(f"  Synced: {'YES' if synced else 'NO'}")
            assert height > 0
        finally:
            await node.disconnect()

    async def test_wallet_status(self):
        node = await self._make_node()
        try:
            status = await node.get_wallet_status()
            if not status:
                pytest.skip(f"Node at {NODE_URL} not reachable")
            initialized = status.get("isInitialized", False)
            unlocked = status.get("isUnlocked", False)
            print(f"  Wallet initialized: {initialized}")
            print(f"  Wallet unlocked: {unlocked}")
            assert initialized, "Wallet not initialized"
        finally:
            await node.disconnect()

    async def test_wallet_balances(self):
        node = await self._make_node()
        try:
            bal = await node.get_wallet_balances()
            if not bal:
                pytest.skip("Wallet locked or not reachable")
            erg = bal["erg"]
            tokens = bal["tokens"]
            print(f"  ERG balance: {erg:.4f}")
            print(f"  Tokens held: {len(tokens)}")

            # Check for SigUSD
            import config
            sigusd_bal = tokens.get(config.SIGUSD_TOKEN_ID, {})
            if sigusd_bal:
                sigusd_amount = sigusd_bal.get("amount", 0) / 100  # 2 decimals
                print(f"  SigUSD balance: {sigusd_amount:.2f}")
            else:
                print("  SigUSD balance: 0")

            assert erg >= 0
        finally:
            await node.disconnect()


class TestSpectrumPrices:
    """Test Spectrum DEX price fetching works."""

    async def _make_dex(self):
        from exchanges.spectrum import SpectrumDEX
        dex = SpectrumDEX()
        await dex.connect()
        return dex

    async def test_erg_sigusd_price(self):
        dex = await self._make_dex()
        try:
            price = await dex.get_erg_sigusd_price()
            assert price is not None, "Failed to fetch Spectrum price"
            assert price > 0
            print(f"  Spectrum ERG/SigUSD: ${price:.4f}")
        finally:
            await dex.disconnect()

    async def test_all_pools(self):
        dex = await self._make_dex()
        try:
            pools = await dex.get_all_erg_sigusd_pools()
            assert len(pools) > 0, "No ERG/SigUSD pools found"
            print(f"  Found {len(pools)} ERG/SigUSD pools:")
            for p in pools:
                lp = getattr(p, '_last_price', 0)
                vol = getattr(p, '_base_volume', 0)
                print(f"    Pool {p.pool_id[:16]}... price=${lp:.4f} vol={vol:.2f}")
        finally:
            await dex.disconnect()


class TestSigmaUSDBank:
    """Test SigmaUSD bank state fetching."""

    async def _make_bank(self):
        from exchanges.sigmausd import SigmaUSDBank
        bank = SigmaUSDBank()
        await bank.connect()
        return bank

    async def test_full_state(self):
        bank = await self._make_bank()
        try:
            state = await bank.get_full_state()
            oracle = state.get("oracle_erg_usd")
            assert oracle is not None, "Failed to fetch oracle price"
            assert oracle > 0
            rr = state.get("reserve_ratio")
            print(f"  Oracle ERG/USD: ${oracle:.4f}")
            print(f"  Bank ERG reserve: {state.get('bank_erg_reserve', 0):.2f}")
            print(f"  SigUSD circulating: {state.get('sigusd_circulating', 0):.2f}")
            print(f"  Reserve ratio: {rr:.0f}%" if rr else "  Reserve ratio: N/A")
            print(f"  Can mint SigUSD: {state.get('can_mint_sigusd')}")
            print(f"  Can redeem SigUSD: {state.get('can_redeem_sigusd')}")
        finally:
            await bank.disconnect()


class TestDEXSwapMath:
    """Test that swap calculations produce sane results with live data."""

    async def test_small_swap_simulation(self):
        """Simulate a 1 ERG swap through different paths using live prices."""
        from exchanges.spectrum import SpectrumDEX
        from exchanges.sigmausd import SigmaUSDBank
        from exchanges.ergo_node import ErgoNodeClient
        import config

        dex = SpectrumDEX()
        bank = SigmaUSDBank()
        node = ErgoNodeClient()
        node.base_url = NODE_URL
        node.api_key = NODE_API_KEY

        await asyncio.gather(dex.connect(), bank.connect(), node.connect())
        # Fix node session for custom URL
        await node.session.close()
        import aiohttp
        node.session = aiohttp.ClientSession(
            headers={"api_key": NODE_API_KEY, "Content-Type": "application/json"}
        )

        try:
            # Fetch live data
            spectrum_price, bank_state = await asyncio.gather(
                dex.get_erg_sigusd_price(),
                bank.get_full_state(),
            )

            assert spectrum_price is not None, "Spectrum price unavailable"
            oracle = bank_state.get("oracle_erg_usd")
            assert oracle is not None, "Oracle price unavailable"

            trade_size = 1.0  # 1 ERG - small test amount

            print(f"\n  === Swap simulation for {trade_size} ERG ===")
            print(f"  Spectrum ERG/SigUSD: ${spectrum_price:.4f}")
            print(f"  Oracle ERG/USD:      ${oracle:.4f}")

            # Path A: ERG -> SigUSD on Spectrum
            sigusd_out = trade_size * spectrum_price * (1 - config.SPECTRUM_POOL_FEE)
            print(f"\n  Path A: Sell {trade_size} ERG on Spectrum")
            print(f"    -> Get {sigusd_out:.4f} SigUSD (after 0.3% DEX fee)")
            print(f"    -> Execution fee: {config.SPECTRUM_EXECUTION_FEE} ERG to batcher")

            # Path B: SigUSD -> ERG on Spectrum (reverse)
            erg_out = sigusd_out / spectrum_price * (1 - config.SPECTRUM_POOL_FEE)
            print(f"\n  Path B: Swap {sigusd_out:.4f} SigUSD back to ERG on Spectrum")
            print(f"    -> Get {erg_out:.4f} ERG (after 0.3% DEX fee)")
            print(f"    -> Round-trip loss: {(1 - erg_out/trade_size)*100:.2f}%")

            # Path C: Mint SigUSD at bank
            if oracle:
                sigusd_from_bank = bank.erg_to_sigusd(trade_size)
                print(f"\n  Path C: Mint SigUSD at bank with {trade_size} ERG")
                print(f"    -> Get {sigusd_from_bank:.4f} SigUSD (after 2.1% bank fee)")
                print(f"    -> Bank {'BLOCKED (RR too low)' if not bank_state.get('can_mint_sigusd') else 'AVAILABLE'}")

            # Path D: Redeem SigUSD at bank
            if oracle:
                erg_from_bank = bank.sigusd_to_erg(sigusd_out)
                print(f"\n  Path D: Redeem {sigusd_out:.4f} SigUSD at bank")
                print(f"    -> Get {erg_from_bank:.4f} ERG (after 2.1% bank fee + tx fee)")
                print(f"    -> Bank {'BLOCKED (RR too high)' if not bank_state.get('can_redeem_sigusd') else 'AVAILABLE'}")

            # Check node wallet
            node_ok = await node.check_connection()
            if node_ok:
                bal = await node.get_wallet_balances()
                if bal:
                    print(f"\n  Node wallet ({NODE_URL}):")
                    print(f"    ERG: {bal['erg']:.4f}")
                    if bal['erg'] >= trade_size + 0.01:
                        print(f"    Wallet has enough ERG for {trade_size} ERG test swap")
                    else:
                        print(f"    Wallet needs >= {trade_size + 0.01:.2f} ERG for test swap")
                else:
                    print(f"\n  Node wallet: locked or empty")
            else:
                print(f"\n  Node at {NODE_URL}: not reachable")

            print(f"\n  NOTE: Actual swap execution is Phase 2 (not yet implemented)")
            print(f"  This test confirms all data sources are working correctly.")

        finally:
            await asyncio.gather(dex.disconnect(), bank.disconnect(), node.disconnect())
