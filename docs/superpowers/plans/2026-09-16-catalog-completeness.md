# Catalog Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a partial contract catalog from rejecting a valid early subscription while preserving strict rejection after the full catalog is available.

**Architecture:** Keep `catalog_ready` as the backward-compatible search-readiness signal and add `catalog_complete` for strict local validation. Built-in catalogs and incremental parsing only make search ready; a full cache or completed download makes the catalog complete. A partial-catalog miss remains a recoverable `TqClientError`, so the existing subscription fallback sends the normalized symbol to the market server.

**Tech Stack:** Python 3, `unittest`, asyncio, Chaquopy mirrored Python sources.

---

## File Structure

- Modify: `tqdiff/client.py` - catalog lifecycle, flags, and `get_instrument` validation.
- Modify: `mobile-app/app/src/main/python/tqdiff/client.py` - byte-identical Android mirror.
- Modify: `tests/test_tqdiff.py` - partial/complete catalog miss regressions.
- Verify: `tools/check_core_drift.py` - mirror equality.

### Task 1: Define the Regression Cases

**Files:**
- Modify: `tests/test_tqdiff.py`

- [ ] **Step 1: Write failing direct-client tests**

Add these imports:

```python
import asyncio

from tqdiff.client import DiffClient, SymbolNotFoundError, TqClientError
```

Add this test class before the module's `__main__` block:

```python
class CatalogCompletenessTests(unittest.TestCase):
    def _partial_client(self) -> DiffClient:
        client = DiffClient("acc", "pwd")
        client._file_loaded.set()
        client._symbol_file = {"SHFE.rb2610": {"symbol": "SHFE.rb2610"}}
        return client

    def test_partial_catalog_miss_is_recoverable(self):
        client = self._partial_client()
        with self.assertRaises(TqClientError) as raised:
            asyncio.run(client._get_instrument("SHFE.au2612"))
        self.assertNotIsInstance(raised.exception, SymbolNotFoundError)
        self.assertFalse(client.catalog_complete)

    def test_complete_catalog_miss_is_strictly_rejected(self):
        client = self._partial_client()
        client._catalog_complete.set()
        with self.assertRaises(SymbolNotFoundError):
            asyncio.run(client._get_instrument("SHFE.au2612"))
        self.assertTrue(client.catalog_complete)
```

- [ ] **Step 2: Run the focused test to verify the failure**

Run:

```powershell
python -m unittest tests.test_tqdiff.CatalogCompletenessTests -v
```

Expected: the partial-miss case raises `SymbolNotFoundError`; the complete-state case also fails because `_catalog_complete` does not exist.

### Task 2: Implement Two Catalog States

**Files:**
- Modify: `tqdiff/client.py`
- Modify: `mobile-app/app/src/main/python/tqdiff/client.py`

- [ ] **Step 1: Add independent completion state and property**

After `self._file_loaded = threading.Event()` in `DiffClient.__init__`, add:

```python
        # 可搜索与完整可校验是不同状态：内置表/增量解析只能前者。
        self._catalog_complete = threading.Event()
```

Replace the current `catalog_ready` property with this compatible property pair:

```python
    @property
    def catalog_ready(self) -> bool:
        """是否已有可搜索的目录记录（兼容既有状态接口）。"""
        return self._file_loaded.is_set()

    @property
    def catalog_complete(self) -> bool:
        """完整目录是否已加载，可据此严格判定本地未命中。"""
        return self._catalog_complete.is_set()
```

- [ ] **Step 2: Mark completion only for a full cache or successful full download**

In the cache branch of `_load_symbol_file`, set completion immediately after search readiness:

```python
        if cached is not None:
            with self._data_lock:
                self._symbol_file = cached
            self._file_loaded.set()
            self._catalog_complete.set()
            self._status(f"合约目录就绪（缓存，{len(cached)} 个合约）")
            return
```

In the successful `download_symbol_file` branch, add the same one line:

```python
                with self._data_lock:
                    self._symbol_file = symbols
                self._file_loaded.set()
                self._catalog_complete.set()
                self._set_catalog_progress(None)
```

Do not set `_catalog_complete` in the built-in catalog branch or `_on_index_progress`. Download failure keeps any search-ready records but leaves strict validation disabled.

- [ ] **Step 3: Change only the missing-record branch of `_get_instrument`**

Replace its final miss handling with:

```python
        record = self._file_entry(symbol)
        if record is None:
            if not self._catalog_complete.is_set():
                raise TqClientError("合约目录尚未完整，交由行情服务确认")
            raise SymbolNotFoundError(f"合约不存在或查询失败：{symbol}")
        return dict(record)
```

Do not modify `InstrumentManager.get` or `SubscriptionManager.subscribe`: the former already propagates recoverable `TqClientError`, and the latter already delegates those cases to the market-server subscription path.

- [ ] **Step 4: Run the focused regression test**

Run:

```powershell
python -m unittest tests.test_tqdiff.CatalogCompletenessTests -v
```

Expected: both tests pass.

- [ ] **Step 5: Mirror the shared file and verify strict equality**

Copy the finalized root `tqdiff/client.py` contents to `mobile-app/app/src/main/python/tqdiff/client.py` using the repository's normal patch workflow. Then run:

```powershell
python tools/check_core_drift.py --strict
```

Expected: exit code 0 and `tqdiff/client.py` is reported as `一致`.

- [ ] **Step 6: Commit the isolated catalog fix**

```powershell
git add tests/test_tqdiff.py tqdiff/client.py mobile-app/app/src/main/python/tqdiff/client.py
git commit -m "fix: distinguish partial contract catalogs"
```

Expected: one commit containing only catalog semantics, test coverage, and the mirror update.

### Task 3: Run the Shared-Core Sweep

**Files:**
- Verify: `tests/`
- Verify: `tools/check_core_drift.py`

- [ ] **Step 1: Run all Python tests**

```powershell
python -m unittest discover -s tests -v
```

Expected: every test passes, including `CatalogCompletenessTests`.

- [ ] **Step 2: Repeat the strict drift check**

```powershell
python tools/check_core_drift.py --strict
```

Expected: exit code 0 with no unexpected drift.

- [ ] **Step 3: Preserve exact totals for the release report**

Record the full-suite test count and strict-drift result in the later candidate verification report. Do not commit command output alone.

