# SolarFlow Phase 1 — Workflow Rebuild

This version is the first clean rebuild of the SolarFlow web application.

## Scope
- Live Oracle 19c connection
- Dashboard backed by Oracle
- Customer create/read/update/delete/search
- Site create/read/update/delete/search
- Validation and Oracle error handling
- Foreign-key-aware deletion
- No database recreation or sample-data insertion

## Existing database
- User: SOLAR_ADMIN
- DSN: localhost:1521/ORCLPDB

## Run
```cmd
py -m pip install -r requirements.txt
py app.py
```

Open http://127.0.0.1:5000

Health check:
http://127.0.0.1:5000/health

Test Customer and Site workflows completely before extending the application to Survey, Design, Installation, Service, Energy, Warranty and Billing.
