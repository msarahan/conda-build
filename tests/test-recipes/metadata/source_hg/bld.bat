if not exist .hg exit 1
hg id
if errorlevel 1 exit 1
for /f "delims=" %%i in ('hg id') do set hgid=%%i
if errorlevel 1 exit 1
echo "%hgid%"
if not "%hgid%"=="0d15c0b5fc78 (some-branch) tip" exit 1
if not exist test exit 1
