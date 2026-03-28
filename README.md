# MNPI Guard — Insider Trading Surveillance System

---

## 📌 Project Overview

MNPI Guard is a compliance surveillance web application designed to detect potential insider trading by monitoring the flow of **Material Non-Public Information (MNPI)**. It tracks document access, employee trades, and automatically correlates suspicious activity — surfacing real-time alerts in a unified dashboard.

---

## 📌 Purpose

The purpose of MNPI Guard is to help compliance teams identify and investigate potential misuse of confidential, market-moving information within an organization. By analyzing document access patterns and trade activity, it flags suspicious correlations such as an employee accessing a confidential merger document and then trading in the same stock shortly after.

---

## 🚀 Run the Frontend

Navigate to the `frontend` directory, install dependencies, and start the dev server:

```bash
cd frontend
npm install
npm run dev
```

The frontend will be available at **`http://localhost:3000`**

---

## 🚀 Run the Backend

Navigate to the `backend` directory, set up the Python virtual environment, install dependencies, and start the server:

```bash
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Start the backend server (from the **repo root**):

```bash
npm run dev
```

This concurrently starts:
- **Backend:** `http://localhost:8000`
- **Frontend:** `http://localhost:3000`
- **API Docs (Swagger):** `http://localhost:8000/docs`

---

## ✅ Recommended Run Flow (Current Logic)

Use one command from the repo root for local development:

```bash
npm run dev
```

This is the recommended flow because the frontend and backend are expected to be available together.

---

## 🔍 Availability Checks (Before Testing Upload/API)

Run these checks from PowerShell in the repo root:

1. Check if frontend/backend ports are listening:

```powershell
Get-NetTCPConnection -LocalPort 3000,8000 -State Listen
```

Expected:
- Port `3000` listening (Next.js frontend)
- Port `8000` listening (FastAPI backend)

2. Check backend health endpoint:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health | Select-Object -ExpandProperty Content
```

Expected response:

```json
{"ok":true}
```

3. Optional: verify frontend route is up in browser:
- `http://localhost:3000`

---

## 🌐 Frontend-Backend API Logic (Current)

The frontend now uses a resilient API strategy:

1. First attempt:
- Uses `NEXT_PUBLIC_API_BASE_URL` when set.

2. Automatic fallback:
- If direct API call fails and `NEXT_PUBLIC_API_BASE_URL` is set, frontend retries through Next.js rewrite proxy (`/api/*`), which routes to `API_PROXY_TARGET`.

3. If both fail:
- UI shows a clear error indicating backend is unreachable and to start FastAPI on port `8000`.

This behavior prevents many local-dev failures caused by stale direct backend URLs.

---

## 🎯 Objectives

- Develop a real-time MNPI detection and compliance surveillance system.
- Automatically score documents for MNPI sensitivity using NLP-based analysis.
- Monitor and flag suspicious employee trades correlated with document access.
- Provide a unified dashboard with alerts, risk scores, and investigation tools.
- Enable compliance officers to investigate insider trading timelines per employee.

---

## 🛠️ Technologies Used

| Layer | Technology |
|---|---|
| **Frontend** | Next.js 15, React 19, TypeScript |
| **Backend** | Python, FastAPI |
| **Database** | SQLite, SQLAlchemy ORM |
| **ML/NLP** | scikit-learn, NumPy |
| **PDF Parsing** | PyPDF |
| **Graph Visualization** | Cytoscape.js |

### Backend Dependencies
| Package | Purpose |
|---|---|
| `fastapi` | High-performance API framework |
| `SQLAlchemy` | ORM for database operations |
| `pydantic` | Data validation and serialization |
| `scikit-learn` | Anomaly detection for trade scoring |
| `pypdf` | PDF text extraction |

### Frontend Dependencies
| Package | Purpose |
|---|---|
| `next` | React framework with SSR |
| `react` | UI component library |
| `cytoscape` | Graph-based correlation visualization |
| `typescript` | Type-safe development |

---

## 📦 Features

- **MNPI Document Scanner** — Upload documents (PDF, TXT) and get instant MNPI risk scores with entity extraction.
- **Trading Monitor** — Import employee trade data via CSV and score trades for anomalies.
- **Risk Correlation Engine** — Automatically correlates document access timestamps with subsequent trades to detect insider patterns.
- **Real-Time Alerts Dashboard** — Surfaces high-severity MNPI and correlation alerts with filtering and resolution tracking.
- **Employee Investigation View** — Timeline-based investigation tool showing document access → trade sequences per employee.
- **Demo Seed Data** — One-click demo data generation for showcasing the system.
- **Auto-Flagging** — Automatically creates alerts when an employee trades a stock related to a restricted document they recently accessed.
- **Swagger API Docs** — Full interactive API documentation at `/docs`.

---

## 📊 Project Deliverables

- **MNPI Detection Panel** — Document upload with automated sensitivity scoring and entity extraction.
- **Trading Monitor Panel** — CSV import, trade listing, and per-trade risk/anomaly scores.
- **Risk Correlation Panel** — Graph visualization of employee-document-trade relationships using Cytoscape.js.
- **Alerts Feed Panel** — Chronological alert stream with severity indicators, type badges, and resolve actions.
- **Employee Investigation Page** — Drill-down view correlating document access times with trade execution times.

---

## 🔧 Environment Variables

**Backend** (`backend/.env`, optional):

```env
DATABASE_URL=sqlite:///./mnpi_guard.db
CORS_ORIGINS=http://localhost:3000
STORAGE_DIR=./storage
```

**Frontend** (`frontend/.env.local`, optional):

```env
NEXT_PUBLIC_API_BASE_URL=
API_PROXY_TARGET=http://127.0.0.1:8000
```

Use `NEXT_PUBLIC_API_BASE_URL` only when you intentionally want direct browser calls to the backend. For local development, keep it empty so the Next.js proxy handles `/api/*` calls consistently.

---

## 🧯 Quick Troubleshooting

If you see errors like `Unable to reach backend API` or `Failed to fetch`:

1. Confirm port `8000` is listening.
2. Confirm `http://127.0.0.1:8000/health` returns `{"ok":true}`.
3. Keep `NEXT_PUBLIC_API_BASE_URL=` (empty) in `frontend/.env.local` for proxy-first local development.
4. Restart frontend after env changes:

```bash
npm run frontend
```

---

## 👥 Team Members

- **Srishanth Reddy** — *(Idea & UI/UX)*
- **S.Akshit** — *(Backend)*
- **Satvik Sai** — *(Backend)*
- **Yadunandan** — *(Frontend)*

