Debug a trading issue: $ARGUMENTS

## Issue Analysis

1. **Parse Issue Description**:
   - Identify component (backend/frontend/integration)
   - Determine affected feature
   - Note error messages or unexpected behavior

2. **Gather Context**:
   - Recent changes to codebase
   - Environment (dev/staging/prod)
   - Reproducibility steps

## Investigation Steps

### Backend Issues

1. **Check Logs**:
   ```bash
   # Application logs
   docker-compose logs -f backend --tail=100
   
   # Celery worker logs
   docker-compose logs -f celery-worker --tail=100
   ```

2. **Database State**:
   ```bash
   # Connect to database
   docker-compose exec postgres psql -U user -d polymarket
   
   # Check relevant tables
   SELECT * FROM orders WHERE created_at > NOW() - INTERVAL '1 hour';
   SELECT * FROM positions WHERE user_id = 'X';
   ```

3. **Redis/Cache State**:
   ```bash
   docker-compose exec redis redis-cli
   KEYS market:*
   GET market:{id}
   ```

4. **API Testing**:
   ```bash
   # Test endpoint
   curl -X GET http://localhost:8000/api/v1/markets/{id}
   
   # Check response time
   time curl -s http://localhost:8000/health
   ```

5. **Polymarket API Status**:
   ```python
   from py_clob_client.client import ClobClient
   client = ClobClient("https://clob.polymarket.com")
   print(client.get_ok())  # Should return OK
   print(client.get_server_time())  # Check connectivity
   ```

### Frontend Issues

1. **Browser Console**:
   - Check for JavaScript errors
   - Network tab for failed requests
   - React DevTools for component state

2. **Build Issues**:
   ```bash
   npm run typecheck
   npm run lint
   npm run build 2>&1 | head -50
   ```

### Integration Issues

1. **WebSocket Connection**:
   - Verify connection established
   - Check message flow
   - Test reconnection logic

2. **Data Flow**:
   - Trace request from frontend to backend
   - Verify database writes
   - Check cache invalidation

## Common Issues & Solutions

### Order Execution Failures

1. Check USDC balance
2. Verify token allowances
3. Check order parameters (price, size bounds)
4. Verify API credentials

### Price Feed Issues

1. Check WebSocket connection status
2. Verify market is active
3. Check rate limiting
4. Fallback to REST API

### Position Sync Issues

1. Compare on-chain positions with database
2. Check for missed events
3. Force position reconciliation

## Fix Implementation

1. **Identify Root Cause**:
   - Document findings
   - Trace to specific code

2. **Implement Fix**:
   - Make minimal necessary changes
   - Add error handling if missing
   - Add logging for future debugging

3. **Add Regression Test**:
   - Create test that would catch this issue
   - Ensure test fails without fix
   - Verify test passes with fix

4. **Verify Fix**:
   - Run full test suite
   - Manual testing of affected flow
   - Check edge cases

## Documentation

1. **Update Error Handling**:
   - Add specific error messages
   - Improve logging

2. **Add to Known Issues** (if applicable):
   - Document in docs/troubleshooting.md
   - Include symptoms and resolution

## Commit

```
fix({scope}): {brief description}

Problem: {what was broken}
Cause: {root cause}
Solution: {how it was fixed}

Closes #{issue_number}
```
