from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Security, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import base64
import asyncio
import json
import os
from openai import AsyncOpenAI
import logging
import time
import uuid
import uvicorn

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global REQUEST_SEMAPHORE
    REQUEST_SEMAPHORE = asyncio.Semaphore(3)
    yield
    REQUEST_SEMAPHORE = None


app = FastAPI(title="AI多模态解题与DeepSeek裁判系统", lifespan=lifespan)

# ================= 鉴权逻辑 =================
security = HTTPBearer()

async def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    token = credentials.credentials
    valid_token = os.getenv("API_TOKEN", "ai_boss_2026")

    if token != valid_token:
        raise HTTPException(
            status_code=401,
            detail="无效的访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token

# ================= 1. 动态秘钥池配置 =================
qwen_keys = os.getenv("QWEN_KEYS", "你的默认千问key").split(",")
glm_keys = os.getenv("GLM_KEYS", "你的默认GLMkey").split(",")
doubao_keys = os.getenv("DOUBAO_KEYS", "你的默认豆包key").split(",")
deepseek_keys = os.getenv("DEEPSEEK_KEYS", "你的默认DeepSeekkey").split(",")

API_TIMEOUT = int(os.getenv("API_TIMEOUT", "90"))

FAILURE_MARKER = "[请求失败]"

REQUEST_SEMAPHORE = None


_key_index = {}

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
                model="qwen3-vl-flash",
                messages=[
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
        return response.choices[0].message.content
    except Exception as e:
        elapsed = time.time() - t0
        logger.warning(f"[{request_id}] 千问 FAIL {elapsed:.1f}s: {e}")
        return f"{FAILURE_MARKER}: {e}"


async def ask_glm(base64_image, prompt, request_id=""):
    t0 = time.time()
    client = get_client("https://open.bigmodel.cn/api/paas/v4")
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="glm-4.6v-flashx",
                messages=[
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
        return response.choices[0].message.content
    except Exception as e:
        elapsed = time.time() - t0
        logger.warning(f"[{request_id}] GLM FAIL {elapsed:.1f}s: {e}")
        return f"{FAILURE_MARKER}: {e}"

async def ask_doubao(base64_image, prompt, request_id=""):
    t0 = time.time()
    client = get_client("https://ark.cn-beijing.volces.com/api/v3")
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="doubao-seed-2-0-mini-260428",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是一个严谨的教育辅助AI。你只输出结构化的解题内容，绝不输出任何客套、废话、开场白或结束语。\n\n"
                            "格式强约束：\n"
                            "1. 所有数学公式必须使用标准 LaTeX，行内用 $...$，独立公式用 $$...$$。\n"
                            "2. 绝对禁止使用 Markdown 加粗符号（**）。\n"
                            "3. 如果是选择题，必须逐项分析 A、B、C、D 四个选项的对错原因，绝不允许只解释正确选项。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}},
                            {"type": "text", "text": (
                                "请识别图片中的题目，并严格按照以下结构输出：\n\n"
                                "【答案】: (仅输出最终选项或结果)\n"
                                "【解答】:\n"
                                "[思路点拨] (限50字以内，仅概括核心考点)\n"
                                "[题目详解] (要求步骤极简，直接列式计算)\n\n"
                                "请直接输出，以“【答案】”开头："
                            )}
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
        return response.choices[0].message.content
    except Exception as e:
        elapsed = time.time() - t0
        logger.warning(f"[{request_id}] 豆包 FAIL {elapsed:.1f}s: {e}")
        return f"{FAILURE_MARKER}: {e}"



async def judge_answers(comparison_text, survivor_count, request_id=""):
    client = get_client("https://api.deepseek.com/v1")

    if survivor_count == 1:
        task_desc = "当前仅有 1 份答案，你无法进行交叉比对。请仅做格式清洗（LaTeX 标准化），并直接采纳该答案。"
        consistency_default = "false"
        warning_req = "请在 warning 中输出：⚠️ 此结果为孤证，未经交叉验证，仅供参考，请务必人工核对。"
    else:
        task_desc = f"当前有 {survivor_count} 份答案，请比对它们的一致性。"
        consistency_default = "true 或 false（视实际一致性而定）"
        warning_req = "如三份不一致，简述分歧点；一致则留空。"

    system_prompt = (
        "你是一个严谨的数据质检与排版专家。请比对提供的解题答案。\n"
        "你必须严格输出一个 JSON 对象，不要输出任何额外的解释，也不要使用 Markdown 代码块（不要输出 ```json），直接以 { 开始，以 } 结束。\n\n"
        "JSON 格式如下：\n"
        "{\n"
        '  "is_consistent": ' + consistency_default + ',\n'
        '  "final_answer": "生成答案过程（注意：要求给出具体的答案核心推导或结论过程，不要只给一个最终结果）",\n'
        '  "final_explanation": "【思路点拨】\\nXXXX\\n\\n【题目解析】\\nXXXX"\n'
        "}\n\n"
        "内容业务规范（核心触线红线）：\n"
        "1. 【题目解析】部分必须严格做到以下三点：\n"
        "   - 不出错：解答内容每一步均不能出错，要求无错别字、无病句；必须符合当前学段的学科规范。\n"
        "   - 不跳步：解答过程要逻辑清晰，步骤严谨，不能出现影响理解的跳步，绝对不能直接使用经验性结论。\n"
        "   - 不超纲：所用知识点与解题方法均在对应学段的教材中学习过，不可使用超纲内容。\n"
        "2. 【思路点拨】部分：根据题干分析问题，帮助学生打开思路，找到解题方法，在思路点拨中明确计算方法、运算规则等。\n\n"
        "排版清洗强约束：\n"
        "1. 必须清除原始文本中所有用于强调的 Markdown 加粗符号（**）。\n"
        "2. 图文混排时，行内数学公式必须严格使用 $ 包裹（如 $x=2$），独立段落的数学公式必须严格使用 $$ 包裹。\n"
        "3. 确保输出的纯 LaTeX 语法正确无误，禁止中文标点混入公式内部。"
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
            return json.loads(raw_result)
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
        prompt_text = """你是一个严谨的教育辅助AI。请识别图片中的题目，如果是选择题，必须逐项分析 A、B、C、D 四个选项的对错原因，绝不允许只解释正确选项。请严格按照以下结构输出，拒绝任何多余的废话和客套：

        【答案】: (仅输出最终选项或结果)
        【解答】:
        [思路点拨] (限50字以内，仅概括核心考点)
        [题目详解] (要求步骤极简，直接列式计算)

        格式强约束（违规将导致系统崩溃）：
        1. 所有数学公式必须使用标准 LaTeX，行内用 $...$，独立公式用 $$...$$。
        2. 绝对禁止在数学公式内外使用 Markdown 加粗符号（**）。"""

        logger.info(f"[{request_id}] 三路并发呼叫开始")
        task1 = ask_qwen(base64_image, prompt_text, request_id)
        task2 = ask_glm(base64_image, prompt_text, request_id)
        task3 = ask_doubao(base64_image, prompt_text, request_id)
        raw_results = await asyncio.gather(task1, task2, task3)

        # 去名化映射 + 幸存者检测
        all_items = []
        for (public_name, internal_name), raw_content in zip(MODEL_MAP, raw_results):
            is_alive = FAILURE_MARKER not in str(raw_content)
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
                raw_data_for_frontend[item["id"]] = item["content"]
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
            f"【{s['id']}答案】\n{s['content']}" for s in survivors
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


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )
