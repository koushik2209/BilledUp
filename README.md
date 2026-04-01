# BilledUp

AI-powered GST billing for Indian shops via WhatsApp.
Bill smarter. Grow faster.

## What it does
Shopkeeper types a message in Telugu/Hindi/English.
BilledUp generates a legally valid GST bill PDF in 10 seconds.

## Setup

1. Clone this repo
2. Install dependencies:
   pip install -r requirements.txt

3. Copy environment file:
   copy .env.example .env

4. Add your Claude API key to .env

5. Run:
   python main.py

## File structure
- main.py          — entry point, interactive billing loop
- bill_generator.py — PDF bill generation
- claude_parser.py  — natural language message parsing
- gst_rates.py      — GST rate lookup table
- config.py         — configuration loader

## Tech stack
- Python 3.11+
- Claude API (Anthropic)
- ReportLab (PDF generation)
- SQLite (database)

## Disclaimer
HSN codes and GST rates are based on best-effort lookup.
Always verify with a CA before filing GST returns.