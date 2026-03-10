@echo off
chcp 65001 >nul
call E:\Anaconda\condabin\conda.bat activate arxiv-digest-lab
if errorlevel 1 (
  echo Failed to activate conda env: arxiv-digest-lab
  exit /b 1
)
cmd /k
