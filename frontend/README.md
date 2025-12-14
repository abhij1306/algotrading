# NSE Trading Screener - Frontend

## Quick Start

### 1. Install Dependencies
```bash
cd frontend
npm install
```

### 2. Run Development Server
```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000)

### 3. Build for Production
```bash
npm run build
npm start
```

## Features

### ✅ Implemented
- **3 Tabs**: Combined, Intraday, Swing
- **Sortable Table**: Click column headers to sort
- **Score Filter**: Slider to filter by minimum score
- **CSV Export**: Download filtered results
- **Loading States**: Spinner while fetching data
- **Error Handling**: Retry button on failure
- **Responsive Design**: Works on mobile and desktop
- **Color-Coded Scores**:
  - Green (≥80): Strong signal
  - Yellow (60-79): Moderate signal
  - Gray (<60): Weak signal
- **Breakout Badges**: "BO" tag for 20-day breakouts

### 📊 Stats Bar
- Total stocks screened
- Features computed
- Intraday/Swing/Combined counts
- Processing time

## Components

### `app/page.tsx`
Main screener page with tabs, stats, and data fetching

### `components/ScreenerTable.tsx`
Sortable data table with color-coded scores

### `components/FilterBar.tsx`
Score slider filter with count display

### `components/ExportButton.tsx`
CSV export with timestamp

## Tech Stack
- **Next.js 16** (App Router)
- **React 18**
- **TypeScript**
- **Tailwind CSS**
- **Turbopack** (dev server)

## API Integration
Connects to backend at `http://localhost:8000/api/screener`

Make sure backend is running before starting frontend!

## Deployment
Deploy to Vercel:
```bash
vercel deploy
```

Or build static export:
```bash
npm run build
```
