@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   웨이브A 빌더 - 실행
echo ========================================
echo.

:: venv 있으면 사용 (Python 3.11/3.12 + rembg 권장)
if exist venv\Scripts\activate.bat (
  call venv\Scripts\activate.bat
  set PY=python
) else (
  set PY=python
  py -c "exit(0)" 2>nul && set PY=py
)
%PY% -c "exit(0)" 2>nul || (
  echo Python이 설치되어 있지 않습니다.
  echo 1. https://python.org 접속 후 설치
  echo 2. 설치 시 "Add Python to PATH" 체크
  echo 3. PC 재시작 후 다시 실행
  start https://www.python.org/downloads/ 2>nul
  pause
  exit /b 1
)

:: 1) 패키지 설치
echo [1/2] 패키지 설치 중...
if exist requirements.txt (
  %PY% -m pip install -q -r requirements.txt 2>nul
) else (
  %PY% -m pip install -q fastapi "uvicorn[standard]" 2>nul
)
if errorlevel 1 (
  echo pip 실패. %PY% -m pip install -r requirements.txt
  pause
  exit /b 1
)

:: 2) 3초 후 브라우저 자동 열기
start /B cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000/"

:: 3) 서버 실행 (이 창을 닫으면 서버도 종료됩니다)
echo [2/2] 서버 실행 중... http://localhost:8000
echo.
echo 브라우저가 곧 열립니다. 열리지 않으면 직접 접속하세요.
echo 종료: Ctrl+C 또는 이 창 닫기
echo ========================================
%PY% -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 (
  echo.
  echo 서버 시작 실패. 터미널에서: %PY% -m uvicorn backend.main:app --port 8000
  pause
)
