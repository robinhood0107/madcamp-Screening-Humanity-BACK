import uvicorn
import os
import shutil
from dotenv import load_dotenv

if __name__ == "__main__":
    # [역할]
    # 로컬 개발 실행 진입점.
    # env 파일/로컬 mock 디렉터리를 준비한 뒤 uvicorn을 띄운다.
    #
    # [주의]
    # 운영 배포에서는 보통 이 파일 대신 process manager/docker entrypoint를 쓴다.
    # 여기 로직은 "개발 편의" 쪽이라서 운영 정책과 섞이지 않게 유지하는 게 중요하다.
    load_dotenv()
    # Ensure env file exists
    if not os.path.exists(".env"):
        print("Creating .env from .env.example")
        shutil.copy(".env.example", ".env")
        
    # 개발용 mock 디렉터리 준비.
    # 없으면 생성만 하고, 기존 파일은 건드리지 않는다.
    os.makedirs("./shared_models_mock", exist_ok=True)
    os.makedirs("./user_assets_mock", exist_ok=True)
    os.makedirs("./uploads", exist_ok=True)
    
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
