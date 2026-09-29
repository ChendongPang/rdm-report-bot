FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
# 只装 headless shell（不装完整版 chromium，省 ~390MB），只装中文字体（不装
# --with-deps 附带的日文/泰文/emoji 等全套字体，省 ~50MB）。
# libnspr4/libnss3/libasound2t64 是 chromium-headless-shell 实测缺的动态库
# （用 ldd 排查确认，见调试记录），没有用 playwright install-deps 的完整列表。
RUN pip install -r requirements.txt \
    && playwright install chromium-headless-shell \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         libnspr4 libnss3 libasound2t64 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY fill_progress.py llm_helper.py work_log.xlsx ./

RUN mkdir -p /app/screenshots

# 账号密码在容器启动后交互式输入（getpass，不落盘），所以必须用 docker run -it 运行
ENTRYPOINT ["python", "fill_progress.py"]
