from fastapi import FastAPI, UploadFile, File
import base64
import asyncio
import json
import os # 新增：引入系统环境变量库
from openai import AsyncOpenAI
import uvicorn

app = FastAPI(title="AI多模态解题与DeepSeek裁判系统")

# ================= 1. 秘钥配置区域 (改用 os.getenv 获取) =================
# 千问配置
QWEN_API_KEY = os.getenv("QWEN_API_KEY")
qwen_client = AsyncOpenAI(api_key=QWEN_API_KEY, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

# GLM 配置
GLM_API_KEY = os.getenv("GLM_API_KEY")
glm_client = AsyncOpenAI(api_key=GLM_API_KEY, base_url="https://open.bigmodel.cn/api/paas/v4")

# 豆包 配置
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY")
doubao_client = AsyncOpenAI(api_key=DOUBAO_API_KEY, base_url="https://ark.cn-beijing.volces.com/api/v3")

# DeepSeek 配置
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
deepseek_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")

# ====================================================================
async def ask_qwen(base64_image, prompt):
    try:
        response = await qwen_client.chat.completions.create(
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
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"[千问请求失败]: {e}"

async def ask_glm(base64_image, prompt):
    try:
        response = await glm_client.chat.completions.create(
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
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"[GLM请求失败]: {e}"

async def ask_doubao(base64_image, prompt):
    try:
        response = await doubao_client.chat.completions.create(
            model="doubao-seed-2-0-mini-260428",
            messages=[
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
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"[豆包请求失败]: {e}"

# 新增：DeepSeek 裁判逻辑
async def judge_answers(qwen_ans, glm_ans, doubao_ans):
    system_prompt = """你是一个严谨的数据质检与排版专家。请比对提供的三份解题答案。
    你必须严格输出一个 JSON 对象，不要输出任何额外的解释，也不要使用 Markdown 代码块（不要输出 ```json），直接以 { 开始，以 } 结束。

    JSON 格式如下：
    {
    "is_consistent": true或false,
    "final_answer": "多数一致时的正确答案 / 三份全冲突时填'需人工核查'",
    "final_explanation": "综合三方最优的最终解答过程 / 全冲突时填'逻辑冲突'",
    "warning": "如果三份不一致，简述分歧点；一致留空"
    }

    排版清洗强制要求（作用于 final_explanation）：
    1. 必须清除原始文本中所有用于强调的 Markdown 加粗符号（**）。
    2. 图文混排时，行内数学公式必须严格使用 $ 包裹（如 $x=2$），独立段落的数学公式必须严格使用 $$ 包裹。
   3. 确保输出的纯 LaTeX 语法正确无误，禁止中文标点混入公式内部。"""

    user_prompt = f"【千问答案】\n{qwen_ans}\n\n【GLM答案】\n{glm_ans}\n\n【豆包答案】\n{doubao_ans}"

    try:
        response = await deepseek_client.chat.completions.create(
            model="deepseek-v4-pro", # 使用 DeepSeek 核心模型
            response_format={"type": "json_object"}, # 强制要求大模型吐出 JSON 格式
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1 # 裁判不需要发散思维，温度调低保证客观严谨
        )
        
        # 将字符串转为真实的 JSON 字典
        raw_result = response.choices[0].message.content
        return json.loads(raw_result)
        
    except Exception as e:
        return {"is_consistent": False, "error": f"[DeepSeek裁判失效]: {e}"}


@app.post("/solve")
async def solve_problem(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        base64_image = base64.b64encode(contents).decode('utf-8')
        prompt_text = """你是一个严谨的教育辅助AI。请识别图片中的题目，严格按照以下结构输出，拒绝任何多余的废话和客套：

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
        results = await asyncio.gather(task1, task2, task3)

        qwen_answer = results[0]
        glm_answer = results[1]
        doubao_answer = results[2]

        print("2. 三个模型解答完毕，已唤醒 DeepSeek 进行三方比对...")
        final_judgement = await judge_answers(qwen_answer, glm_answer, doubao_answer)

        print("3. 比对完成，返回最终结果！")
        return {
            "status": "success",
            "raw_data": {
                "qwen": qwen_answer,
                "glm": glm_answer,
                "doubao": doubao_answer
            },
            "final_result": final_judgement
        }
        
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)