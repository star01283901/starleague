@echo off
echo [star] Installing dependencies...
pip install pyinstaller --quiet
pip install -r requirements.txt --quiet

echo [star] Building exe...
python -m PyInstaller --onefile ^
  --name "star" ^
  --icon "star2.png" ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --add-data "plugins;plugins" ^
  --add-data "star1.png;." ^
  --add-data "star2.png;." ^
  --hidden-import flask ^
  --hidden-import psutil ^
  --hidden-import requests ^
  --hidden-import rich ^
  app.py

echo.
echo [star] Done. EXE is in dist\star.exe
pause
