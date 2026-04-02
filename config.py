import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# ── Claude API ──
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# ── Platform details ──
PLATFORM_NAME     = os.getenv("PLATFORM_NAME",     "BilledUp")
PLATFORM_TAGLINE  = os.getenv("PLATFORM_TAGLINE",  "Bill smarter. Grow faster.")
PLATFORM_SUPPORT  = os.getenv("PLATFORM_SUPPORT_PHONE", "+91 99999 99999")

# ── Database ──
DATABASE_URL      = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError(
        "\n"
        "ERROR: DATABASE_URL is missing\n"
        "Steps to fix:\n"
        "  Local:      Add DATABASE_URL=sqlite:///billedup.db to your .env file\n"
        "  Production: Set DATABASE_URL to your PostgreSQL connection string\n"
    )

# ── App ──
# Production safety: force DEBUG=False if any production env indicator is set,
# even if DEBUG=True was set accidentally.
_env = os.getenv("RAILWAY_ENVIRONMENT", "") or os.getenv("FLASK_ENV", "") or os.getenv("ENV", "")
if _env.lower() == "production":
    DEBUG = False
else:
    DEBUG = os.getenv("DEBUG", "False") == "True"
PORT              = int(os.getenv("PORT", 5000))

# ── Bill settings ──
BILL_PREFIX       = "INV"

# ── Validate on startup ──
if not ANTHROPIC_API_KEY:
    raise ValueError(
        "\n"
        "ERROR: ANTHROPIC_API_KEY is missing\n"
        "Steps to fix:\n"
        "1. Go to platform.anthropic.com\n"
        "2. Create an API key\n"
        "3. Add it to your .env file\n"
    )

if not PLATFORM_NAME:
    raise ValueError("PLATFORM_NAME missing in .env file")

if not PLATFORM_SUPPORT:
    raise ValueError("PLATFORM_SUPPORT_PHONE missing in .env file")


def get_config_summary():
    """Returns a printable summary of current config."""
    return {
        "platform": PLATFORM_NAME,
        "tagline": PLATFORM_TAGLINE,
        "support": PLATFORM_SUPPORT,
        "database": DATABASE_URL,
        "debug": DEBUG,
        "port": PORT,
        "bill_prefix": BILL_PREFIX,
        "api_key_loaded": bool(ANTHROPIC_API_KEY),
    }
# ── Meta WhatsApp Cloud API ──
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_ACCESS_TOKEN    = os.getenv("WHATSAPP_ACCESS_TOKEN")
VERIFY_TOKEN             = os.getenv("VERIFY_TOKEN")

# ── Public base URL ──
BASE_URL = os.getenv("BASE_URL", "https://web-production-91c36.up.railway.app")


# ── Lazy Anthropic client singleton ──
_anthropic_client = None

def get_anthropic_client():
    """Return a shared Anthropic client instance (created on first call)."""
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


if __name__ == "__main__":
    summary = get_config_summary()
    print("=" * 45)
    print(f"  {summary['platform']} — {summary['tagline']}")
    print("=" * 45)
    print(f"  Support  : {summary['support']}")
    print(f"  Database : {summary['database']}")
    print(f"  Debug    : {summary['debug']}")
    print(f"  Port     : {summary['port']}")
    print(f"  API Key  : {ANTHROPIC_API_KEY[:8]}...{'*' * 12}")
    print("=" * 45)
    print("  Config loaded successfully")
    print("=" * 45)