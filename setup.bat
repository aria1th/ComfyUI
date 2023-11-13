
if not exist venv (
    python -m venv venv
)

set PYTHON_EXE="venv/Scripts/python.exe"
%PYTHON_EXE% -m pip install --upgrade pip

set PIP_EXE="venv/Scripts/pip.exe"
%PIP_EXE% install -r requirements.txt

for /d %%d in (custom_nodes/*) do (
    if exist %%d\requirements.txt (
        %PIP_EXE% install -r %%d\requirements.txt
    )
)

