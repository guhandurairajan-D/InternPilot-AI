# InternPilot AI — Flask + SQLite starter

## Requirements
- Python 3.10+
- VS Code

## Run on Windows (PowerShell)
1. Extract this folder and open it in VS Code (`File > Open Folder`).
2. Open `Terminal > New Terminal`.
3. Create a virtual environment:
   ```powershell
   python -m venv .venv
   ```
4. Activate it:
   ```powershell
   .venv\Scripts\Activate.ps1
   ```
   If PowerShell blocks activation, use Command Prompt terminal and run `.venv\Scripts\activate.bat`, or continue using `.venv\Scripts\python.exe -m pip` commands.
5. Install Flask:
   ```powershell
   python -m pip install -r requirements.txt
   ```
6. Start the app:
   ```powershell
   python app.py
   ```
7. Open http://127.0.0.1:5000 in your browser. Keep the terminal open while using it.
8. Check http://127.0.0.1:5000/api/health — it should return `status: ok`.

## What works
- Existing HTML interface served by Flask
- Seeded sample interns
- Task assignment saved to `internpilot.db` (SQLite)
- Task list and intern statuses loaded from the database
- Submission text review saved to SQLite with a transparent keyword/rubric baseline
- Text-file uploads can be loaded into the review field

## Important limitations
- The review logic is a simple rubric baseline, not a real generative AI model.
- Reminder dates are stored, but no background scheduler or actual email/SMS delivery is configured.
- Sample intern names and email addresses are demo data.
- This starter is for local development, not production deployment. Do not put real sensitive intern submissions into an unsecured demo.

## Useful database tables
- `interns`: sample intern profiles
- `tasks`: assignment requirements, deadlines, language and reminder lead time
- `reviews`: submitted text, rubric score and feedback
