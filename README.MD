python3 -m pip install fastapi uvicorn requests twilio
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

or
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000


uv venv --python 3.11                      
source .venv/bin/activate
uv pip install fastapi uvicorn requests twilio "uvicorn[standard]" motor python-multipart
uvicorn main:app --host 0.0.0.0 --port 8000                   

