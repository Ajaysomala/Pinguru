#!/bin/bash
# ============================================================
# PinGuru — GitHub Push Script
# Run this from your local machine after downloading the files
# ============================================================

set -e

echo "🚀 Setting up PinGuru on GitHub..."

# ── BACKEND REPO ─────────────────────────────────────────────
cd pinguru

git init
git add .
git commit -m "feat: initial PinGuru backend — FastAPI + MongoDB + Instagram API"

# Create repo on GitHub (requires GitHub CLI — gh auth login first)
# gh repo create pinguru --private --source=. --push

# OR manually:
echo ""
echo "📌 MANUAL STEPS:"
echo "1. Go to github.com/new"
echo "2. Create repo named: pinguru (Private)"
echo "3. Run these commands:"
echo ""
echo "   git remote add origin https://github.com/YOUR_USERNAME/pinguru.git"
echo "   git branch -M main"
echo "   git push -u origin main"

cd ..

# ── LANDING PAGE REPO ────────────────────────────────────────
cd pinguru-landing

git init
git add .
git commit -m "feat: PinGuru landing page"

echo ""
echo "📌 LANDING PAGE STEPS:"
echo "1. Go to github.com/new"
echo "2. Create repo named: pinguru-landing (Public — needed for GitHub Pages)"
echo "3. Run:"
echo ""
echo "   git remote add origin https://github.com/YOUR_USERNAME/pinguru-landing.git"
echo "   git branch -M main"
echo "   git push -u origin main"
echo ""
echo "4. Go to repo Settings → Pages → Source: Deploy from branch → main → / (root)"
echo "5. Your landing page will be live at: https://YOUR_USERNAME.github.io/pinguru-landing"
echo "   OR point pinguru.me to it via Namecheap CNAME"

echo ""
echo "✅ Done! Next: claim DO credits → deploy backend → set webhook"
