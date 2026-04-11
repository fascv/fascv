# Module Workspaces

Each runtime module has an isolated workspace for a dedicated Codex instance.

- `modules/md`
- `modules/core`
- `modules/exec`
- `modules/journal`
- `modules/control`
- `modules/impact_console`

Shared contracts live in `shared/src/trading_shared`.
Launcher app workspace is `apps/launch`.

Example (from repo root) for module-local tooling/imports:

```bash
PYTHONPATH=modules/core/src:shared/src python3 -c "import trading_core; print(trading_core.__all__)"
```
