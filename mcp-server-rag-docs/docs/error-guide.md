# Common Deployment Errors

## uvicorn not found

Symptom:
- Running server command fails with 'uvicorn: command not found'.

Fix:
1. Activate your virtual environment.
2. Install dependencies with pip install -e .
3. Re-run the startup command.

## Address already in use

Symptom:
- Startup fails with port conflict.

Fix:
1. Stop old process on the same port.
2. Or run with another port.

## Permission denied when reading logs

Symptom:
- Log file read returns permission error.

Fix:
1. Use a file under your user-owned workspace.
2. Run terminal as user with proper permission.
