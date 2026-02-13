# Troubleshooting Guide

## Common Issues

### Issue 1: WebSocket "Sometimes Works, Sometimes Doesn't"

**Symptoms:**
- WebSocket connects successfully
- Some symbols receive live data, others don't
- Inconsistent behavior across page reloads

**Root Cause:**
Symbol format mismatch - some symbols getting converted correctly, others not.

**Solution:**
1. Check symbol format being sent from frontend:
   ```javascript
   // In browser console
   console.log("Subscribing to:", symbols);
   // Should be: ['SBIN', 'RELIANCE']
   // NOT: ['NSE:SBIN-EQ', 'NSE:RELIANCE-EQ']
   ```

2. Verify backend conversion:
   - Check backend logs for "Subscribed to X symbols".
   - It should now automatically use `symbol_master.to_fyers()`.

3. If still failing, check `symbol_master` logic:
   ```python
   from app.services.symbol_master import symbol_master

   # Test specific symbol
   symbol = "PROBLEMATIC_SYMBOL"
   print(symbol_master.to_fyers(symbol))
   ```

### Issue 2: Screener Shows Symbols But WebSocket Fails

**Symptoms:**
- Screener loads and displays symbols
- Clicking subscribe does nothing
- No errors in console

**Root Cause:**
WebSocket not converting symbols before subscription (Fixed by SymbolMaster integration).

**Solution:**
1. Verify `LiveMarketService.subscribe()` method uses `symbol_master.batch_to_fyers()`.
2. Check market hours:
   - Live data is only available during Indian market hours (9:15 AM - 3:30 PM IST).
   - Use `DEV_MODE=True` in `.env` to bypass market hour checks for testing.

### Issue 3: Import Errors After Reorganization

**Symptoms:**
```
ImportError: cannot import name 'X' from 'scripts'
```

**Root Cause:**
Scripts moved to `data_platform/` or `backend/scripts/`.

**Solution:**
1. Use the new paths:
   - Pipelines: `data_platform.pipelines`
   - Processors: `data_platform.processors`
   - Validators: `data_platform.validators`
2. Check `scripts/archive/ARCHIVE_INVENTORY.md` to find where a script went.

## Prevention Checklist

Before pushing code, verify:
- [ ] All symbols in DB are DB_FORMAT (no colons or dashes).
- [ ] All WebSocket subscriptions use `symbol_master.to_fyers()`.
- [ ] All API responses use `symbol_master.to_display()`.
- [ ] Startup validation passes.
- [ ] Tests pass: `pytest tests/test_symbol_flow.py`.

## Getting Help

1. Review `docs/ARCHITECTURE.md` for system overview.
2. Review `docs/SYMBOL_FORMAT_RULES.md` for format rules.
3. Check backend logs: `backend/startup.log`.
