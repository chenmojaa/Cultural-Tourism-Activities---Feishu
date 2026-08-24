from openai import OpenAI
import requests
import asyncio
import httpx

def al_model_test():
    # _base_url="https://vllmqwen3.dy.takin.cc/v1"
    # _base_url="http://bge-host:8000/v1"
    # _base_url="https://666666.dy.takin.cc/v1"
    # _base_url = "https://777777.dy.takin.cc/v1"
    _base_url = "http://36.7.147.231:8000/v1"

    client = OpenAI(
        base_url=_base_url,
        api_key="abc123",
    )

    resp = client.chat.completions.create(
        # model="/data/models/Qwen3-Next-80B-A3B-Instruct-AWQ-4bit",
        # model="/data/models/Qwen2.5-32B-Instruct-AWQ-4bit",
        model="/data/models/Qwen3.5-35B-A3B-FP8",
        # model="/data/models/Qwen3-VL-32B-Thinking",
        messages=[
            {"role": "user", "content": "帮我写一句欢迎语"}
        ],
        extra_body={
            "chat_template_kwargs": {
                "enable_thinking": False
            }
        }
    )
    print(resp)
    print(f"_base_url = {_base_url}")
    print(f"模型输出结果：{resp.choices[0].message.content}")

async def al_model_test22():
    # _base_url="https://vllmqwen3.dy.takin.cc/v1"
    # _base_url="http://bge-host:8000/v1"
    # _base_url="https://666666.dy.takin.cc/v1"
    _base_url = "https://777777.dy.takin.cc/v1/chat/completions"

    headers = {"Authorization": f"Bearer abc123", "Content-Type": "application/json"}
    payload = {
        "model": "/data/models/Qwen3.5-35B-A3B-FP8",
        "messages": [{"role": "user", "content": "帮我写一句欢迎语"}],
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    timeout = 300.0
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(_base_url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    choices = data.get("choices") or []
    print(choices)



def victor_model_test():
    _base_url = "http://36.7.147.231:8002/v1/score"
    # _base_url = "http://bge-host:8002/v1/score"
    # 相似度得分
    response = requests.post(
        _base_url,
        headers={
            "Authorization": "Bearer abc123",
            "Content-Type": "application/json"
        },
        json={
            "model": "/data/models/bge-m3",
            "text_1": ["什么是人工智能"],
            "text_2": ["AI是人工智能的缩写"]
        }
    )
    data = response.json()
    print(f"——————————————————————————————————————————————————————————————————————————————————————————————")
    print(f"相似度得分结果：{data['data'][0]['score']}")

    # 文本转向量
    response = requests.post(
        "http://10.0.0.253:8002/v1/embeddings",  # 改为embeddings接口
        headers={
            "Authorization": "Bearer abc123",
            "Content-Type": "application/json"
        },
        json={
            "model": "/data/models/bge-m3",
            "input": "什么是人工智能",  # 改为input参数
            # "dimensions": 1024  # 指定输出维度：512/256/128/64
        }
    )

    data = response.json()
    print(f"——————————————————————————————————————————————————————————————————————————————————————————————")
    print(f"文本转向量结果：{data}")
    # 提取向量
    embeddings = [item["embedding"] for item in data["data"]]
    print(f"向量维度: {len(embeddings[0])}")
    print(f"向量数量: {len(embeddings)}")
    print(f"向量值: {embeddings[0]}")




def qwen_video_test():
    _base_url = "https://777777.dy.takin.cc/v1/chat/completions"
    client = OpenAI(
        base_url=_base_url,
        api_key="abc123",
    )

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "video_url",
                    "video_url": {
                        "url": "https://qianwen-res.oss-accelerate.aliyuncs.com/Qwen3.5/demo/video/N1cdUjctpG8.mp4"
                    }
                },
                {
                    "type": "text",
                    "text": "How many porcelain jars were discovered in the niches located in the primary chamber of the tomb?"
                }
            ]
        }
    ]

    # When vLLM is launched with `--media-io-kwargs '{"video": {"num_frames": -1}}'`,
    # video frame sampling can be configured via `extra_body` (e.g., by setting `fps`).
    # This feature is currently supported only in vLLM.
    #
    # By default, `fps=2` and `do_sample_frames=True`.
    # With `do_sample_frames=True`, you can customize the `fps` value to set your desired video sampling rate.
    chat_response = client.chat.completions.create(
        model="Qwen/Qwen3.5-35B-A3B",
        messages=messages,
        max_tokens=81920,
        temperature=1.0,
        top_p=0.95,
        presence_penalty=1.5,
        extra_body={
            "top_k": 20,
            "mm_processor_kwargs": {"fps": 2, "do_sample_frames": True},
        },
    )

    print("Chat response:", chat_response)



if __name__ == '__main__':
    # al_model_test()
    victor_model_test()
    # asyncio.run(al_model_test22())
    # qwen_video_test()
