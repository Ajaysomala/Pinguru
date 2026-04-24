# Backend Production Release Gate

## Must Pass Before Deploy

1. `pytest -q` passes (including security enforcement tests).
2. `pip-audit` returns no unresolved high/critical findings.
3. `bandit -r app -lll -ii` has no high-confidence high-severity findings.
4. Webhook signature checks are active in production.
5. CSRF and origin checks active for cookie-auth mutation routes.

## Plan Contract Controls

- Free: 5 automation flows
- Free: 500 contacts/month
- Starter: 15 automation flows
- Pro: unlimited flows
- Feature gates enforced server-side

## Evidence

- CI workflow run link
- Security test output
- Release notes with rollback plan

Deploy only when all checks are green.
