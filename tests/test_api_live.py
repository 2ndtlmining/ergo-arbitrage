"""Live API integration tests. Requires valid credentials in .env.

Run with: python -m pytest tests/test_api_live.py -v -s
Use -k to run specific tests: python -m pytest tests/test_api_live.py -k "nonkyc" -v -s
"""
import pytest
import config

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not config.NONKYC_API_KEY, reason="NONKYC_API_KEY not set"),
    pytest.mark.asyncio,
]


# ==================== NonKYC Public ====================

class TestNonKYCPublic:
    async def _make_exchange(self):
        from exchanges.nonkyc import NonKYCExchange
        ex = NonKYCExchange()
        await ex.connect()
        return ex

    async def test_get_erg_usdt_price(self):
        ex = await self._make_exchange()
        try:
            quote = await ex.get_price("ERG/USDT")
            assert quote is not None
            assert quote.bid > 0
            assert quote.ask > 0
            assert quote.ask >= quote.bid
            print(f"  ERG/USDT: bid=${quote.bid:.4f} ask=${quote.ask:.4f}")
        finally:
            await ex.disconnect()

    async def test_get_orderbook(self):
        ex = await self._make_exchange()
        try:
            ob = await ex.get_orderbook("ERG/USDT", depth=10)
            assert ob is not None
            assert len(ob.bids) > 0
            assert len(ob.asks) > 0
            print(f"  Orderbook: {len(ob.bids)} bids, {len(ob.asks)} asks")
            print(f"  Best bid: ${ob.best_bid.price:.6f} x {ob.best_bid.quantity}")
            print(f"  Best ask: ${ob.best_ask.price:.6f} x {ob.best_ask.quantity}")
        finally:
            await ex.disconnect()

    async def test_get_withdraw_fee(self):
        ex = await self._make_exchange()
        try:
            fee = await ex.get_withdraw_fee("ERG")
            assert fee > 0
            print(f"  ERG withdrawal fee: {fee} ERG")
        finally:
            await ex.disconnect()

    async def test_orderbook_effective_prices(self):
        ex = await self._make_exchange()
        try:
            ob = await ex.get_orderbook("ERG/USDT", depth=20)
            assert ob is not None
            sell_1 = ob.effective_sell_price(1)
            if sell_1:
                print(f"  Sell 1 ERG effective: ${sell_1:.6f}")
                assert sell_1 > 0
        finally:
            await ex.disconnect()


# ==================== NonKYC Authenticated ====================

class TestNonKYCAuthenticated:
    async def _make_exchange(self):
        from exchanges.nonkyc import NonKYCExchange
        ex = NonKYCExchange()
        await ex.connect()
        return ex

    async def test_get_balances(self):
        ex = await self._make_exchange()
        try:
            balances = await ex.get_all_balances()
            assert isinstance(balances, list)
            print(f"  Non-zero balances: {len(balances)}")
            for b in balances:
                print(f"    {b['asset']}: {b['available']}")
        finally:
            await ex.disconnect()

    async def test_get_erg_balance(self):
        ex = await self._make_exchange()
        try:
            balance = await ex.get_balance("ERG")
            assert isinstance(balance, float)
            print(f"  ERG balance: {balance}")
        finally:
            await ex.disconnect()

    async def test_get_usdt_balance(self):
        ex = await self._make_exchange()
        try:
            balance = await ex.get_balance("USDT")
            assert isinstance(balance, float)
            print(f"  USDT balance: {balance}")
        finally:
            await ex.disconnect()


# ==================== Kucoin Public ====================

class TestKucoinPublic:
    async def _make_exchange(self):
        from exchanges.kucoin import KucoinExchange
        ex = KucoinExchange()
        await ex.connect()
        return ex

    async def test_get_erg_usdt_price(self):
        ex = await self._make_exchange()
        try:
            quote = await ex.get_price("ERG/USDT")
            assert quote is not None
            assert quote.bid > 0
            assert quote.ask > 0
            assert quote.ask >= quote.bid
            print(f"  ERG/USDT: bid=${quote.bid:.4f} ask=${quote.ask:.4f}")
        finally:
            await ex.disconnect()

    async def test_get_orderbook(self):
        ex = await self._make_exchange()
        try:
            ob = await ex.get_orderbook("ERG/USDT", depth=20)
            assert ob is not None
            assert len(ob.bids) > 0
            assert len(ob.asks) > 0
            print(f"  Orderbook: {len(ob.bids)} bids, {len(ob.asks)} asks")
            print(f"  Best bid: ${ob.best_bid.price:.6f} x {ob.best_bid.quantity}")
            print(f"  Best ask: ${ob.best_ask.price:.6f} x {ob.best_ask.quantity}")
        finally:
            await ex.disconnect()

    async def test_get_withdraw_fee(self):
        ex = await self._make_exchange()
        try:
            fee = await ex.get_withdraw_fee("ERG")
            assert fee > 0
            print(f"  ERG withdrawal fee: {fee} ERG")
        finally:
            await ex.disconnect()

    async def test_get_24h_stats(self):
        ex = await self._make_exchange()
        try:
            stats = await ex.get_24h_stats()
            assert stats is not None
            assert stats["last"] > 0
            print(f"  Last: ${stats['last']:.4f}")
            print(f"  24h Volume: {stats['volume_erg']:.0f} ERG (${stats['volume_usdt']:.0f})")
            print(f"  24h Change: {stats['change_rate']*100:.2f}%")
        finally:
            await ex.disconnect()

    async def test_fetch_erg_usdt_price(self):
        ex = await self._make_exchange()
        try:
            price = await ex.fetch_erg_usdt_price()
            assert price is not None
            assert price > 0
            print(f"  ERG mid price: ${price:.4f}")
        finally:
            await ex.disconnect()


# ==================== Spectrum DEX ====================

class TestSpectrumDEX:
    async def _make_dex(self):
        from exchanges.spectrum import SpectrumDEX
        dex = SpectrumDEX()
        await dex.connect()
        return dex

    async def test_get_erg_sigusd_price(self):
        dex = await self._make_dex()
        try:
            price = await dex.get_erg_sigusd_price()
            assert price is not None
            assert price > 0
            print(f"  Spectrum ERG/SigUSD: ${price:.4f}")
        finally:
            await dex.disconnect()

    async def test_pool_box(self):
        dex = await self._make_dex()
        try:
            pool = await dex.get_pool_state()
            assert pool is not None, "ERG/SigUSD pool box not found"
            assert pool.reserve_x > 0 and pool.reserve_y > 0
            assert pool.fee_num == 995
            print(f"  Pool {pool.pool_id[:16]}... {pool.reserve_x:,.0f} ERG / {pool.reserve_y:,.2f} SigUSD")
        finally:
            await dex.disconnect()


# ==================== SigmaUSD Bank ====================

class TestSigmaUSDBank:
    async def _make_bank(self):
        from exchanges.sigmausd import SigmaUSDBank
        bank = SigmaUSDBank()
        await bank.connect()
        return bank

    async def test_fetch_oracle_price(self):
        bank = await self._make_bank()
        try:
            price = await bank.fetch_oracle_price()
            assert price is not None
            assert 0.01 < price < 100
            print(f"  Oracle ERG/USD: ${price:.4f}")
        finally:
            await bank.disconnect()

    async def test_fetch_bank_state(self):
        bank = await self._make_bank()
        try:
            ok = await bank.fetch_bank_state()
            assert ok
            assert bank._bank_erg_reserve > 0
            print(f"  Bank ERG reserve: {bank._bank_erg_reserve:.2f}")
            if bank._sigusd_circulating:
                print(f"  SigUSD circulating: {bank._sigusd_circulating:.2f}")
        finally:
            await bank.disconnect()

    async def test_reserve_ratio(self):
        bank = await self._make_bank()
        try:
            await bank.fetch_oracle_price()
            await bank.fetch_bank_state()
            rr = bank.reserve_ratio
            if rr:
                assert rr > 0
                print(f"  Reserve ratio: {rr:.0f}%")
                print(f"  Can mint: {bank.can_mint_sigusd()}")
                print(f"  Can redeem: {bank.can_redeem_sigusd()}")
        finally:
            await bank.disconnect()

    async def test_sigusd_to_erg_conversion(self):
        """Redeem is never RR-restricted; fees are 2% protocol + 0.229% UI."""
        bank = await self._make_bank()
        try:
            await bank.get_full_state()
            if bank.state is None:
                # The bank fails closed when the explorer did not return the on-chain oracle box
                # (bank quotes disabled); that is the designed behaviour, not a fee to measure.
                pytest.skip("explorer did not return the on-chain oracle box; bank quotes disabled")
            erg = bank.sigusd_to_erg(10)
            raw = 10 / bank._oracle_price
            fee_pct = (1 - (erg + 0.0021) / raw) * 100
            assert abs(fee_pct - 2.22) < 0.05
            print(f"  10 SigUSD -> {erg:.4f} ERG (fee: {fee_pct:.2f}%)")
            if not bank.can_mint_sigusd():
                assert bank.erg_to_sigusd(10) == 0
        finally:
            await bank.disconnect()


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
