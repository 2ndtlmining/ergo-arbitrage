"""Live checks against your node (python -m pytest -m live -q). They skip when the node is unreachable."""
import pytest

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


# ==================== Ergo Node ====================

class TestErgoNode:
    async def _make_node(self):
        from exchanges.ergo_node import ErgoNodeClient
        node = ErgoNodeClient()
        await node.connect()
        return node

    async def test_node_reachable(self):
        node = await self._make_node()
        try:
            ok = await node.check_connection()
            if not ok:
                pytest.skip("Ergo node not reachable")
            print("  Node is reachable")
        finally:
            await node.disconnect()

    async def test_node_info(self):
        node = await self._make_node()
        try:
            info = await node.get_node_info()
            if not info:
                pytest.skip("Ergo node not reachable")
            assert "fullHeight" in info
            print(f"  Height: {info['fullHeight']}")
            print(f"  Network: {info.get('network', '?')}")
        finally:
            await node.disconnect()

    async def test_wallet_status(self):
        node = await self._make_node()
        try:
            status = await node.get_wallet_status()
            if not status:
                pytest.skip("Ergo node not reachable")
            print(f"  Initialized: {status.get('isInitialized')}")
            print(f"  Unlocked: {status.get('isUnlocked')}")
        finally:
            await node.disconnect()

    async def test_wallet_balance(self):
        node = await self._make_node()
        try:
            bal = await node.get_wallet_balances()
            if not bal:
                pytest.skip("Ergo node not reachable or wallet locked")
            print(f"  ERG: {bal['erg']:.4f}")
            print(f"  Tokens: {len(bal['tokens'])}")
        finally:
            await node.disconnect()


class TestChainState:
    async def test_pool_bank_and_oracle_are_read_from_the_node(self):
        import aiohttp
        import config
        from ergo.chain_state import prices_from_snapshot, read_snapshot
        headers = {"api_key": config.ERGO_NODE_API_KEY, "Content-Type": "application/json"}
        async with aiohttp.ClientSession(headers=headers) as ns:
            try:
                snap = await read_snapshot(ns)
            except (aiohttp.ClientConnectionError, OSError) as e:
                pytest.skip(f"Ergo node not reachable: {e}")
        prices = prices_from_snapshot(snap)
        assert 0 < prices["spectrum_erg_sigusd"] < 100
        assert prices["bank"]["oracle_erg_usd"] > 0 and prices["bank"]["reserve_ratio"] > 0
