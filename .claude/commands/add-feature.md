Add a new trading feature to the application: $ARGUMENTS

## Analysis Phase

1. Parse the feature requirements from arguments
2. Identify required components:
   - Backend: models, schemas, services, routes, tasks
   - Frontend: components, hooks, stores, types
   - Database: new tables or columns
   - External APIs: Polymarket endpoints needed

## Backend Implementation

1. **Database Layer** (if needed):
   - Create SQLAlchemy model in app/models/
   - Create Pydantic schemas in app/schemas/
   - Generate alembic migration: `alembic revision --autogenerate -m "Add {feature}"`
   - Apply migration: `alembic upgrade head`

2. **Service Layer**:
   - Create service class in app/services/
   - Implement business logic with proper error handling
   - Add type hints to all functions
   - Include docstrings in Google format

3. **API Layer**:
   - Create router in app/api/routes/
   - Add endpoints following REST conventions
   - Include proper response models
   - Add OpenAPI documentation

4. **Background Tasks** (if async processing needed):
   - Create Celery task in app/tasks/
   - Configure task routing and retry logic

5. **Tests**:
   - Create test file in tests/
   - Write unit tests for service layer
   - Write integration tests for API endpoints
   - Target >80% coverage for new code

## Frontend Implementation

1. **Types**:
   - Add TypeScript interfaces in src/types/

2. **API Service**:
   - Add API functions in src/services/

3. **State Management** (if needed):
   - Create Zustand store in src/stores/

4. **React Query Hooks**:
   - Create custom hooks in src/hooks/

5. **Components**:
   - Create component files in appropriate directory
   - Use TailwindCSS for styling
   - Implement loading and error states
   - Make components accessible

## Quality Checks

1. Run backend tests: `pytest -v`
2. Run type checking: `mypy app/`
3. Run linting: `ruff check app/`
4. Run frontend tests: `npm run test`
5. Run frontend type check: `npm run typecheck`

## Documentation

1. Update API documentation if needed
2. Add feature documentation to docs/
3. Update CLAUDE.md if new patterns are introduced

## Commit

Create a conventional commit with format:
```
feat(scope): description

- Implementation details
- Breaking changes if any
```
