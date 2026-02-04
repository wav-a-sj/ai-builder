@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   웨이브A - 프론트엔드만 실행 (백엔드 없음)
echo ========================================
echo.
echo 쇼핑/영상 기능은 동작하지 않습니다.
echo 전체 기능: start.bat 사용
echo ========================================
echo.

timeout /t 2 /nobreak >nul
start "" "http://localhost:5500/index.html"
python -m http.server 5500 -b 127.0.0.1
pause
