// Points the frontend at the backend API. Same file is used by the web app
// and the Capacitor-wrapped mobile app - just edit this one value.
//
// - Local dev: keep the localhost default and run the backend with
//   `uvicorn app.main:app --reload` (see README).
// - Deployed: replace with your Lambda Function URL (from `sam deploy`).
const API_BASE = 'http://localhost:8000';
