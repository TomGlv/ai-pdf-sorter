@echo off
chcp 65001 >nul
set /p SOURCE=Dossier des PDF a trier : 
set /p SORTIE=Dossier ou ranger les PDF tries : 
python "%~dp0trier_pdfs.py" "%SOURCE%" "%SORTIE%"
pause
