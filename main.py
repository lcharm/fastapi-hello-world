from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Security, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
import base64
import asyncio
import json
import os
from openai import AsyncOpenAI
import logging
import time
import uuid
import textwrap
import uvicorn

from auth import init_db, authenticate_user, decode_jwt, is_user_active

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global REQUEST_SEMAPHORE
    init_db()
    REQUEST_SEMAPHORE = asyncio.Semaphore(3)
    yield
    REQUEST_SEMAPHORE = None


app = FastAPI(title="AI多模态解题与DeepSeek裁判系统", lifespan=lifespan)

# ================= 鉴权逻辑 =================
security = HTTPBearer()

async def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    token = credentials.credentials

    # 1) 尝试 JWT 解码
    username = decode_jwt(token)
    if username is not None:
        if not is_user_active(username):
            raise HTTPException(
                status_code=401,
                detail="账号已被禁用",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return username

    # JWT 解码失败，拒绝请求
    raise HTTPException(
        status_code=401,
        detail="无效的访问令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )

# ================= 登录接口 =================

@app.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    token = authenticate_user(form_data.username, form_data.password)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误，或账号已被禁用",
        )
    return {"access_token": token, "token_type": "bearer"}


# ================= 1. 动态秘钥池配置 =================
qwen_keys = os.getenv("QWEN_KEYS", "你的默认千问key").split(",")
glm_keys = os.getenv("GLM_KEYS", "你的默认GLMkey").split(",")
doubao_keys = os.getenv("DOUBAO_KEYS", "你的默认豆包key").split(",")
deepseek_keys = os.getenv("DEEPSEEK_KEYS", "你的默认DeepSeekkey").split(",")

API_TIMEOUT = int(os.getenv("API_TIMEOUT", "90"))

REQUEST_SEMAPHORE = None


_key_index = {}

BASE_SYSTEM_PROMPT = textwrap.dedent("""\
    你是一个严谨的解题引擎。请按以下系统约束输出。

    【系统输出红线】：
    1. 公式定界符：行内公式强制用 $ 包裹，独立公式用 $$ 包裹。绝对禁止使用 \\( \\) 或 \\[ \\]。
    2. 禁止加粗：绝对禁止使用 Markdown 加粗符号 (**)。
    3. 禁止废话：禁止任何开场白或结束语。

    【题型处理规范】：
    - 选择题：[题目详解] 中必须逐项分析 ABCD 对错原因。
    - 主观题/解答题：给出无跳步的极简推导过程。

    【强制输出结构】（严格按此顺序，禁止增删节点）：
    【题目判定】: (限20字，明确学段、学科及考点)
    【解答】:
    [思路点拨] (一语道破解题规则或公式)
    [题目详解] (严格按题型处理规范输出)
    【答案】: (仅输出最终纯字母或数值，必须位于文末)
    """)

# 预创建客户端池，避免每次请求重复建连/TLS握手
_CLIENT_POOLS = {
    "https://dashscope.aliyuncs.com/compatible-mode/v1": [
        AsyncOpenAI(api_key=k, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
        for k in qwen_keys
    ],
    "https://open.bigmodel.cn/api/paas/v4": [
        AsyncOpenAI(api_key=k, base_url="https://open.bigmodel.cn/api/paas/v4")
        for k in glm_keys
    ],
    "https://ark.cn-beijing.volces.com/api/v3": [
        AsyncOpenAI(api_key=k, base_url="https://ark.cn-beijing.volces.com/api/v3")
        for k in doubao_keys
    ],
    "https://api.deepseek.com/v1": [
        AsyncOpenAI(api_key=k, base_url="https://api.deepseek.com/v1")
        for k in deepseek_keys
    ],
}


def get_client(base_url):
    """轮转选择预建客户端，不复建 HTTP 连接"""
    idx = _key_index.get(base_url, 0)
    _key_index[base_url] = idx + 1
    pool = _CLIENT_POOLS[base_url]
    return pool[idx % len(pool)]


# ================= 2. 核心调用逻辑 =================
async def ask_qwen(base64_image, prompt, request_id=""):
    t0 = time.time()
    client = get_client("https://dashscope.aliyuncs.com/compatible-mode/v1")
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="qwen3.5-plus",
                messages=[
                    {
                        "role": "system",
                        "content": BASE_SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
                        ]
                    }
                ],
                temperature=0.1,
                max_tokens=4096
            ),
            timeout=API_TIMEOUT
        )
        elapsed = time.time() - t0
        logger.info(f"[{request_id}] 千问 OK {elapsed:.1f}s")
        return {"status": "success", "content": response.choices[0].message.content}
    except Exception as e:
        elapsed = time.time() - t0
        logger.warning(f"[{request_id}] 千问 FAIL {elapsed:.1f}s: {e}")
        return {"status": "error", "content": str(e)}


async def ask_glm(base64_image, prompt, request_id=""):
    t0 = time.time()
    client = get_client("https://open.bigmodel.cn/api/paas/v4")
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="GLM-4.6V",
                messages=[
                    {
                        "role": "system",
                        "content": BASE_SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
                        ]
                    }
                ],
                temperature=0.1,
                max_tokens=16384,
                extra_body={"thinking": {"type": "enabled", "budget_tokens": 4096}}
            ),
            timeout=API_TIMEOUT
        )
        elapsed = time.time() - t0
        logger.info(f"[{request_id}] GLM OK {elapsed:.1f}s")
        return {"status": "success", "content": response.choices[0].message.content}
    except Exception as e:
        elapsed = time.time() - t0
        logger.warning(f"[{request_id}] GLM FAIL {elapsed:.1f}s: {e}")
        return {"status": "error", "content": str(e)}

async def ask_doubao(base64_image, prompt, request_id=""):
    t0 = time.time()
    client = get_client("https://ark.cn-beijing.volces.com/api/v3")
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="Doubao-Seed-2.0-lite",
                messages=[
                    {
                        "role": "system",
                        "content": BASE_SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}},
                            {"type": "text", "text": prompt}
                        ]
                    }
                ],
                temperature=0.1,
                max_tokens=4096
            ),
            timeout=API_TIMEOUT
        )
        elapsed = time.time() - t0
        logger.info(f"[{request_id}] 豆包 OK {elapsed:.1f}s")
        return {"status": "success", "content": response.choices[0].message.content}
    except Exception as e:
        elapsed = time.time() - t0
        logger.warning(f"[{request_id}] 豆包 FAIL {elapsed:.1f}s: {e}")
        return {"status": "error", "content": str(e)}



async def judge_answers(comparison_text, survivor_count, request_id=""):
    client = get_client("https://api.deepseek.com/v1")

    if survivor_count == 1:
        task_desc = "当前仅有 1 份答案，你无法进行交叉比对。请仅做格式清洗（LaTeX 标准化），并直接采纳该答案。"
        consistency_default = "false"
        warning_value = "⚠️ 此结果为孤证，未经交叉验证，仅供参考，请务必人工核对。"
    else:
        task_desc = f"当前有 {survivor_count} 份答案，请比对它们的一致性。"
        consistency_default = "true 或 false（视实际一致性而定）"
        warning_value = f"如 {survivor_count} 份答案存在分歧，简述分歧点；一致则留空。"

    system_prompt = (
        "你是一个数据比对与清洗引擎。你无法看到原图，仅负责多路文本一致性比对统票与排版清洗。\n"
        "你必须严格输出一个 JSON 对象，不要输出任何额外的解释，也不要使用 Markdown 代码块（不要输出 ```json），直接以 { 开始，以 } 结束。\n\n"
        "JSON 格式如下：\n"
        "{\n"
        '  "is_consistent": ' + consistency_default + ',\n'
        '  "final_answer": "必须仅提取最终核心结果，例如纯字母 \'B\' 或纯公式 \'x=2\'，绝对禁止附带 \'故选\' 或其他说明文字。",\n'
        '  "final_explanation": "【思路点拨】\\nXXXX\\n\\n【题目解析】\\nXXXX",\n'
        '  "warning": "' + warning_value + '"\n'
        "}\n\n"
        "内容业务规范：\n"
        "1. 严格区分题型：遇到选择题时，绝对不允许在 final_answer 字段中输出大段文字推导，只能输出选项结论；所有的详细推导过程必须全部放在【题目解析】中。\n"
        "2. 【思路点拨】部分：根据题干分析问题，帮助学生打开思路，找到解题方法。\n\n"
        "排版清洗强约束（违背将直接导致前端页面解析崩溃）：\n"
        "1. 必须清除原始文本中所有用于强调的 Markdown 加粗符号（**）。\n"
        "2. 【致命红线】绝对禁止使用 \\( ... \\) 或 \\[ ... \\] 作为公式定界符！\n"
        "3. 行内数学公式必须严格使用 $ 包裹（如 $x=2$），独立段落的数学公式必须严格使用 $$ 包裹。\n"
        "4. 确保输出的纯 LaTeX 语法正确无误，禁止中文标点混入公式内部。"
    )

    user_prompt = task_desc + "\n\n" + comparison_text

    max_retries = 2
    for attempt in range(max_retries):
        current_prompt = user_prompt
        current_temp = 0.1
        if attempt > 0:
            current_prompt = user_prompt + "\n\n警告：你上一次输出的不是合法的 JSON，请严格检查括号闭合，确保所有花括号、方括号、引号成对出现。"
            current_temp = 0.5

        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model="deepseek-v4-pro",
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": current_prompt}
                    ],
                    temperature=current_temp
                ),
                timeout=API_TIMEOUT
            )
            raw_result = response.choices[0].message.content
            logger.info(f"[{request_id}] DeepSeek 裁判 OK")
            # 清洗层：防御偶发的 markdown 代码块包裹或首尾杂讯
            cleaned = raw_result.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()
            # 定位 JSON 边界，剔除前后可能的解释文本
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                cleaned = cleaned[start:end + 1]
            return json.loads(cleaned)
        except json.JSONDecodeError:
            if attempt < max_retries - 1:
                logger.warning(f"[{request_id}] DeepSeek JSON 解析失败，重试 {attempt + 1}/{max_retries - 1}")
                continue
            return {
                "is_consistent": False,
                "final_answer": "需人工核查",
                "final_explanation": "裁判模型返回了非法 JSON，无法完成比对。",
                "warning": "系统错误：DeepSeek 输出格式异常，请重试。"
            }
        except Exception as e:
            return {
                "is_consistent": False,
                "final_answer": "需人工核查",
                "final_explanation": f"裁判模型调用失败: {e}",
                "warning": "裁判系统不可用，以下为原始模型输出，请人工判断。"
            }


# ================= 3. 路由网关注入鉴权与主逻辑 =================

MODEL_MAP = [
    ("模型 1", "千问"),
    ("模型 2", "GLM"),
    ("模型 3", "豆包"),
]


@app.post("/solve")
async def solve_problem(file: UploadFile = File(...), token: str = Security(verify_token)):
    request_id = str(uuid.uuid4())[:8]
    t_start = time.time()
    logger.info(f"[{request_id}] 收到解题请求")

    if REQUEST_SEMAPHORE is None:
        logger.error(f"[{request_id}] 信号量未初始化，拒绝请求")
        return JSONResponse(status_code=503, content={"status": "error", "message": "服务正在启动，请稍后重试"})

    acquired = False
    try:
        await asyncio.wait_for(REQUEST_SEMAPHORE.acquire(), timeout=10)
        acquired = True
    except asyncio.TimeoutError:
        logger.warning(f"[{request_id}] 服务繁忙，排队超时")
        return JSONResponse(
            status_code=503,
            content={"status": "busy", "message": "服务繁忙，前面还有排队请求，请稍后重试"}
        )

    try:
        contents = await file.read()
        base64_image = base64.b64encode(contents).decode('utf-8')
        # 统一极简用户指令（核心约束见 BASE_SYSTEM_PROMPT）
        user_instruction = "请识别图片并按系统规范解答。"

        logger.info(f"[{request_id}] 三路并发呼叫开始 (统一系统提示词)")
        task1 = ask_qwen(base64_image, user_instruction, request_id)
        task2 = ask_glm(base64_image, user_instruction, request_id)
        task3 = ask_doubao(base64_image, user_instruction, request_id)
        raw_results = await asyncio.gather(task1, task2, task3, return_exceptions=True)

        # 去名化映射 + 幸存者检测
        all_items = []
        for (public_name, internal_name), raw_content in zip(MODEL_MAP, raw_results):
            is_alive = isinstance(raw_content, dict) and raw_content.get("status") == "success"
            all_items.append({
                "id": public_name,
                "internal_name": internal_name,
                "content": raw_content,
                "alive": is_alive,
            })

        survivors = [item for item in all_items if item["alive"]]
        survivor_count = len(survivors)

        # 构建前端 raw_data（去名化）
        raw_data_for_frontend = {}
        for item in all_items:
            if item["alive"]:
                raw_data_for_frontend[item["id"]] = item["content"]["content"]
            else:
                raw_data_for_frontend[item["id"]] = "⚠️ 该模型响应超时或异常，已自动屏蔽。"

        # 0 幸存者：全员阵亡
        if survivor_count == 0:
            elapsed = time.time() - t_start
            logger.error(f"[{request_id}] 全员阵亡 {elapsed:.1f}s")
            return {
                "status": "error",
                "message": "所有模型均响应超时，请检查网络或 API Key 后重试。",
                "raw_data": raw_data_for_frontend,
            }

        # 构造 DeepSeek 比对文本（使用公开名称）
        comparison_text = "\n\n".join(
            f"【{s['id']}答案】\n{s['content']['content']}" for s in survivors
        )

        logger.info(f"[{request_id}] 幸存 {survivor_count} 路，唤醒 DeepSeek 比对...")
        final_judgement = await judge_answers(comparison_text, survivor_count, request_id)

        elapsed = time.time() - t_start
        logger.info(f"[{request_id}] 完成 {elapsed:.1f}s")
        return {
            "status": "success",
            "survivor_count": survivor_count,
            "raw_data": raw_data_for_frontend,
            "final_result": final_judgement,
        }

    except Exception as e:
        elapsed = time.time() - t_start
        logger.error(f"[{request_id}] 异常 {elapsed:.1f}s: {e}")
        return {"status": "error", "message": str(e)}

    finally:
        REQUEST_SEMAPHORE.release()


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "models": {
            "qwen": "configured" if qwen_keys != ["你的默认千问key"] else "unconfigured",
            "glm": "configured" if glm_keys != ["你的默认GLMkey"] else "unconfigured",
            "doubao": "configured" if doubao_keys != ["你的默认豆包key"] else "unconfigured",
            "deepseek": "configured" if deepseek_keys != ["你的默认DeepSeekkey"] else "unconfigured",
        }
    }


# 挂载静态资源目录（前端热更新 + 版本管理）
app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )
