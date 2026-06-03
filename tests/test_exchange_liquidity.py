"""持续流动性过滤的纯函数测试 — sustained_quote_volume.

核心诉求:对"昙花一现"的细币稳健 —— 某天暴量不应抬高它的持续流动性评分。
所以用中位数(median),不用均值;且丢掉最后一根未收盘日线(项目惯例)。
"""
from exchange import sustained_quote_volume


def _kl(qvols):
    """构造最小 1d kline 行,quote volume 在索引 7。"""
    return [[0, 0, 0, 0, 0, 0, 0, v, 0, 0, 0, 0] for v in qvols]


def test_median_over_closed_days():
    # 7 收盘日 + 1 未收盘 → 取前 7 的中位数
    kl = _kl([10e6, 20e6, 30e6, 40e6, 50e6, 60e6, 70e6, 999e6])
    assert sustained_quote_volume(kl, lookback=7) == 40e6  # median of 10..70M


def test_drops_unclosed_last_candle():
    # 最后一根(未收盘)的巨量不参与计算
    kl = _kl([5e6, 5e6, 5e6, 999e6])
    assert sustained_quote_volume(kl, lookback=7) == 5e6


def test_spike_day_does_not_inflate():
    # 平时 1M、某天暴到 100M:median 仍 ~1M(均值会被拉到 ~15M)→ 正确判它为细币
    kl = _kl([1e6, 1e6, 1e6, 1e6, 1e6, 1e6, 100e6, 2e6])  # 末位未收盘被丢
    assert sustained_quote_volume(kl, lookback=7) == 1e6


def test_only_last_lookback_days_used():
    # 给 10 收盘日,lookback=7 只看最近 7 个
    kl = _kl([999e6, 999e6, 999e6, 1e6, 1e6, 1e6, 1e6, 1e6, 1e6, 1e6, 5e6])
    # 最近 7 收盘日(丢末位)= [1,1,1,1,1,1,1]M → median 1M
    assert sustained_quote_volume(kl, lookback=7) == 1e6


def test_fewer_days_than_lookback():
    # 新币只有 3 天历史 → 用现有的算 median,不报错
    kl = _kl([8e6, 12e6, 10e6, 50e6])  # 3 收盘日 + 未收盘
    assert sustained_quote_volume(kl, lookback=7) == 10e6


def test_empty_and_single_return_zero():
    assert sustained_quote_volume([], lookback=7) == 0.0
    assert sustained_quote_volume(_kl([99e6]), lookback=7) == 0.0  # 仅一根未收盘


# ---- _apply_sustained_filter mode 行为(stub data_client,不走网络)----
import exchange as exchange_mod
from exchange import Exchange


def _make_exchange(monkeypatch, vol_by_sym):
    """造一个 Exchange,其 data_client.futures_klines 返回每币固定持续量(7 收盘日都相同)。"""
    ex = Exchange()

    class _Stub:
        def futures_klines(self, symbol, interval, limit):
            v = vol_by_sym[symbol]
            return _kl([v] * 7 + [999e6])  # 7 收盘日 + 1 未收盘
    monkeypatch.setattr(ex, "data_client", _Stub())
    return ex


def _set_cfg(monkeypatch, mode, floor=50e6, lookback=7):
    cfg = exchange_mod.config
    monkeypatch.setattr(cfg, "SUSTAINED_VOLUME_FILTER_MODE", mode)
    monkeypatch.setattr(cfg, "MIN_SUSTAINED_QUOTE_VOLUME", floor)
    monkeypatch.setattr(cfg, "SUSTAINED_VOLUME_LOOKBACK_DAYS", lookback)


def test_enforce_drops_thin(monkeypatch):
    _set_cfg(monkeypatch, "enforce")
    ex = _make_exchange(monkeypatch, {"FATUSDT": 200e6, "THINUSDT": 5e6})
    assert ex._apply_sustained_filter(["FATUSDT", "THINUSDT"]) == ["FATUSDT"]


def test_observe_keeps_all(monkeypatch):
    _set_cfg(monkeypatch, "observe")
    ex = _make_exchange(monkeypatch, {"FATUSDT": 200e6, "THINUSDT": 5e6})
    # observe 只记录、不砍 → 顺序与内容不变
    assert ex._apply_sustained_filter(["FATUSDT", "THINUSDT"]) == ["FATUSDT", "THINUSDT"]


def test_off_is_passthrough(monkeypatch):
    _set_cfg(monkeypatch, "off")
    ex = _make_exchange(monkeypatch, {"THINUSDT": 5e6})
    assert ex._apply_sustained_filter(["THINUSDT"]) == ["THINUSDT"]


def test_fetch_failure_does_not_drop(monkeypatch):
    _set_cfg(monkeypatch, "enforce")
    ex = Exchange()

    class _Boom:
        def futures_klines(self, symbol, interval, limit):
            raise RuntimeError("network down")
    monkeypatch.setattr(ex, "data_client", _Boom())
    # 拉取失败时保守:不砍
    assert ex._apply_sustained_filter(["XUSDT"]) == ["XUSDT"]
