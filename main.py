from fastapi import FastAPI, UploadFile, File, Security, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import base64
import asyncio
import json
import os
import random
from openai import AsyncOpenAI
import uvicorn

app = FastAPI(title="AI多模态解题与DeepSeek裁判系统")

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

API_TIMEOUT = int(os.getenv("API_TIMEOUT", "60"))

FAILURE_MARKER = "[请求失败]"


def get_client(keys_pool, base_url):
    return AsyncOpenAI(api_key=random.choice(keys_pool), base_url=base_url)


# ================= 2. 核心调用逻辑 =================
async def ask_qwen(base64_image, prompt):
    client = get_client(qwen_keys, "https://dashscope.aliyuncs.com/compatible-mode/v1")
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
        return response.choices[0].message.content
    except Exception as e:
        return f"{FAILURE_MARKER}: {e}"


async def ask_glm(base64_image, prompt):
    client = get_client(glm_keys, "https://open.bigmodel.cn/api/paas/v4")
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
                max_tokens=4096
            ),
            timeout=API_TIMEOUT
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"{FAILURE_MARKER}: {e}"


async def ask_doubao(base64_image, prompt):
    client = get_client(doubao_keys, "https://ark.cn-beijing.volces.com/api/v3")
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
        return response.choices[0].message.content
    except Exception as e:
        return f"{FAILURE_MARKER}: {e}"


async def judge_answers(comparison_text, survivor_count):
    client = get_client(deepseek_keys, "https://api.deepseek.com/v1")

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
        '  "final_answer": "多数一致时的正确答案 / 仅一份时采用该答案 / 全冲突时填 需人工核查",\n'
        '  "final_explanation": "综合各方最优的最终解答过程",\n'
        '  "warning": "' + warning_req + '"\n'
        "}\n\n"
        "排版清洗强制要求（作用于 final_explanation）：\n"
        "1. 必须清除原始文本中所有用于强调的 Markdown 加粗符号（**）。\n"
        "2. 图文混排时，行内数学公式必须严格使用 $ 包裹（如 $x=2$），独立段落的数学公式必须严格使用 $$ 包裹。\n"
        "3. 确保输出的纯 LaTeX 语法正确无误，禁止中文标点混入公式内部。"
    )

    user_prompt = task_desc + "\n\n" + comparison_text

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="deepseek-v4-pro",
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1
            ),
            timeout=API_TIMEOUT
        )
        raw_result = response.choices[0].message.content
        return json.loads(raw_result)
    except json.JSONDecodeError:
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

        print("1. 正在同时呼叫千问、GLM、豆包 (三路并发)...")
        task1 = ask_qwen(base64_image, prompt_text)
        task2 = ask_glm(base64_image, prompt_text)
        task3 = ask_doubao(base64_image, prompt_text)
        raw_results = await asyncio.gather(task1, task2, task3)

        # 去名化映射 + 幸存者检测
        all_items = []
        for (public_name, internal_name), raw_content in zip(MODEL_MAP, raw_results):
            is_alive = not isinstance(raw_content, Exception) and FAILURE_MARKER not in str(raw_content)
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
            return {
                "status": "error",
                "message": "所有模型均响应超时，请检查网络或 API Key 后重试。",
                "raw_data": raw_data_for_frontend,
            }

        # 构造 DeepSeek 比对文本（使用公开名称）
        comparison_text = "\n\n".join(
            f"【{s['id']}答案】\n{s['content']}" for s in survivors
        )

        print(f"2. 幸存模型数: {survivor_count}，已唤醒 DeepSeek 进行{'清洗' if survivor_count == 1 else '三方比对'}...")
        final_judgement = await judge_answers(comparison_text, survivor_count)

        print("3. 比对完成，返回最终结果！")
        return {
            "status": "success",
            "survivor_count": survivor_count,
            "raw_data": raw_data_for_frontend,
            "final_result": final_judgement,
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/health")
async def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )
