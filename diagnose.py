#!/usr/bin/env python3
"""
Telegram Music Bot - Diagnostic Tool
Run this to check if everything is configured correctly
"""

import os
import sys
import subprocess

print("="*60)
print("🔍 TELEGRAM MUSIC BOT - DIAGNOSTICS")
print("="*60)
print()

errors = []
warnings = []
success = []

# Check 1: Python version
print("1️⃣  Checking Python version...")
python_version = sys.version_info
if python_version.major == 3 and python_version.minor >= 10:
    success.append(f"✅ Python {python_version.major}.{python_version.minor}.{python_version.micro}")
else:
    errors.append(f"❌ Python version too old: {python_version.major}.{python_version.minor}")
print()

# Check 2: Environment variables
print("2️⃣  Checking environment variables...")
required_vars = ["API_ID", "API_HASH", "BOT_TOKEN", "SESSION_STRING"]
optional_vars = ["OWNER_ID", "PORT", "REDIS_URL"]

for var in required_vars:
    value = os.getenv(var)
    if value:
        # Show only first/last few chars for security
        if len(value) > 20:
            masked = f"{value[:8]}...{value[-8:]}"
        else:
            masked = f"{value[:4]}...{value[-4:]}"
        success.append(f"✅ {var}: {masked}")
    else:
        errors.append(f"❌ {var}: NOT SET (REQUIRED)")

for var in optional_vars:
    value = os.getenv(var)
    if value:
        success.append(f"✅ {var}: Set")
    else:
        warnings.append(f"⚠️  {var}: Not set (optional)")
print()

# Check 3: Required files
print("3️⃣  Checking required files...")
required_files = [
    "main.py",
    "requirements.txt",
    "Dockerfile",
    "health_check.py",
    "start.sh"
]

for file in required_files:
    if os.path.exists(file):
        size = os.path.getsize(file)
        success.append(f"✅ {file}: {size} bytes")
    else:
        errors.append(f"❌ {file}: NOT FOUND")
print()

# Check 4: Dependencies
print("4️⃣  Checking Python dependencies...")
dependencies = [
    "pyrogram",
    "pytgcalls",
    "yt_dlp",
    "aiohttp"
]

for dep in dependencies:
    try:
        __import__(dep)
        # Get version if possible
        try:
            module = __import__(dep)
            version = getattr(module, '__version__', 'unknown')
            success.append(f"✅ {dep}: {version}")
        except:
            success.append(f"✅ {dep}: installed")
    except ImportError:
        errors.append(f"❌ {dep}: NOT INSTALLED")
print()

# Check 5: FFmpeg
print("5️⃣  Checking FFmpeg...")
try:
    result = subprocess.run(
        ['ffmpeg', '-version'],
        capture_output=True,
        text=True,
        timeout=5
    )
    if result.returncode == 0:
        version_line = result.stdout.split('\n')[0]
        success.append(f"✅ FFmpeg: {version_line}")
    else:
        warnings.append("⚠️  FFmpeg found but version check failed")
except FileNotFoundError:
    errors.append("❌ FFmpeg: NOT FOUND (Required for audio streaming)")
except Exception as e:
    warnings.append(f"⚠️  FFmpeg check failed: {e}")
print()

# Check 6: Network connectivity
print("6️⃣  Checking network connectivity...")
try:
    import socket
    socket.create_connection(("api.telegram.org", 443), timeout=5)
    success.append("✅ Telegram API: Reachable")
except Exception as e:
    errors.append(f"❌ Telegram API: Unreachable - {e}")
print()

# Check 7: File permissions
print("7️⃣  Checking file permissions...")
if os.path.exists("start.sh"):
    import stat
    st = os.stat("start.sh")
    if st.st_mode & stat.S_IXUSR:
        success.append("✅ start.sh: Executable")
    else:
        warnings.append("⚠️  start.sh: Not executable (run: chmod +x start.sh)")
print()

# Summary
print("="*60)
print("📊 DIAGNOSTIC SUMMARY")
print("="*60)
print()

if success:
    print("✅ SUCCESS:")
    for msg in success:
        print(f"   {msg}")
    print()

if warnings:
    print("⚠️  WARNINGS:")
    for msg in warnings:
        print(f"   {msg}")
    print()

if errors:
    print("❌ ERRORS:")
    for msg in errors:
        print(f"   {msg}")
    print()
    print("🔧 Fix these errors before deploying!")
    print()
else:
    print("🎉 All checks passed! Ready to deploy.")
    print()

# Exit code
sys.exit(1 if errors else 0)
