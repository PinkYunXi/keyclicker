@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [1/3] 检查依赖...
python -c "import PyQt6, keyboard" 2>nul || (
    echo     正在安装依赖: PyQt6 keyboard
    python -m pip install -r requirements.txt || goto :fail
)
python -c "import PyInstaller" 2>nul || (
    echo     正在安装 PyInstaller
    python -m pip install pyinstaller || goto :fail
)

echo [2/3] 运行测试...
python -m unittest discover -s tests || goto :fail

echo [3/3] 打包 exe...
python -m PyInstaller --noconfirm KeyClicker.spec || goto :fail

echo.
echo 打包完成: %~dp0dist\KeyClicker.exe
goto :eof

:fail
echo.
echo 构建失败，请检查上方错误信息。
exit /b 1
