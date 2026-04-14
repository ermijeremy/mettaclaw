import os
import openai

client = openai.OpenAI(
    api_key=os.environ["ASI_API_KEY"],
    base_url="https://inference.asicloud.cudos.org/v1",
)


def useAsiCloud(model, max_tokens, content):
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=int(max_tokens),
    )
    return (
        resp.choices[0]
        .message.content.replace("_quote_", '"')
        .replace("_apostrophe_", "'")
    )
