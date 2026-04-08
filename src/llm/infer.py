from src.config import load_settings
from src.llm.llm_client import LLMClient
import argparse


if __name__ == "__main__":
    settings = load_settings()
    client = LLMClient(settings.llm.get())

    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", type=str, default=None)
    parser.add_argument("--prompt_file", type=str, default="prompt.md")
    parser.add_argument("--response_file", type=str, default="response.md")
    args = parser.parse_args()

    if args.prompt is not None:
        prompt = args.prompt
    else:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read()

    response = client.single_turn(prompt)

    
    with open(args.response_file, "w", encoding="utf-8") as f:
        f.write(response)

    print(response)