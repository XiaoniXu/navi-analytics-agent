# OpenAI API Setup and Cost Guide

## Do you need ChatGPT Plus?

No. This project uses the OpenAI API, which is billed separately from the ChatGPT web product. A ChatGPT Plus subscription is not required for API calls.

## Suggested setup

1. Sign in to the OpenAI Platform and set up API billing or prepaid credits.
2. Create an API project and API key for this work.
3. Copy `.env.example` to `.env`.
4. Add the key and chosen model name:

```text
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-5-mini
```

5. Keep `.env` on your local machine. It is already listed in `.gitignore`.

## Suggested first model

`gpt-5-mini` is a reasonable starting point for this project because it supports function calling and is priced for high-volume, cost-sensitive text workflows. Check the official model and pricing pages before you add credits, because prices and available models can change.

## How the Agent calls the model

The Agent is not the Streamlit interface itself. Streamlit simply collects a question and displays the result. The Agent uses a loop like this:

```text
1. Send the user question, dashboard context, and approved tool definitions to the model.
2. The model chooses a tool or asks a concise clarification question.
3. Python validates the chosen tool arguments and runs a read-only, parameterized query.
4. Send the aggregate tool result back to the model.
5. Display the final explanation and a compact tool audit.
```

The starter files contain an example orchestration loop. Your work is to implement the warehouse queries and wire them into the Streamlit experience.

## Planning cost estimate

The estimates below use the current planning assumption of about 3,500 input tokens and 300 output tokens per short dashboard question. That covers a typical two-step tool flow: one model turn to select a tool and one to explain the result.

| Usage pattern | Approximate gpt-5-mini cost |
|---|---:|
| One short tool-backed question | About $0.0015 |
| 100 questions | About $0.15 |
| 500 questions | About $0.75 |
| 1,000 questions | About $1.50 |

For ordinary development and a portfolio demo, a small prepaid balance or project budget of roughly $3 to $5 generally provides comfortable room. Costs can increase when prompts, tool outputs, or answers become long.

## Cost-control ideas

- Use a short system prompt and concise tool descriptions.
- Return small aggregate result sets instead of raw event data.
- Cap the final answer length during development.
- Keep web search, file search, image input, and other paid tools out of this assignment.
- Use one API project for this course project, then review usage in the Platform dashboard.
- Consider prepaid billing and review whether auto-recharge is enabled before you start.

## Key safety

Treat your API key like a password. Store it in an environment variable, not in Python source code. Do not share a single API key across students or commit a key to GitHub.

## Official references

- OpenAI API Keys: https://platform.openai.com/api-keys
- OpenAI Function Calling Guide: https://platform.openai.com/docs/guides/function-calling
- OpenAI API Pricing: https://openai.com/api/pricing/
- OpenAI API Projects and Budgets: https://help.openai.com/en/articles/9186755-managing-your-work-in-the-api-platform-with-projects
