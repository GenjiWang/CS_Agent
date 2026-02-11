from unsloth import FastLanguageModel
from transformers import TextStreamer
from datasets import load_dataset
from unsloth.chat_templates import standardize_sharegpt, train_on_responses_only
from trl import SFTTrainer, SFTConfig
import torch

# -----------------------------
# 模型設定
# -----------------------------
max_seq_length = 1024
dtype = None

model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="unsloth/gpt-oss-20b",
    dtype=dtype,
    max_seq_length=max_seq_length,
    load_in_4bit=True,
    full_finetuning=False,
)

model = FastLanguageModel.get_peft_model(
    model,
    r=8,
    target_modules=[
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ],
    lora_alpha=16,
    lora_dropout=0,
    bias="none",
    use_gradient_checkpointing="unsloth",
    random_state=3407,
    use_rslora=False,
    loftq_config=None,
)

# -----------------------------
# 測試推論
# -----------------------------
'''messages = [{"role": "user", "content": "手機進水了應該要怎麼處理"}]
for effort in ["low", "medium", "high"]:
    inputs = tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
        reasoning_effort=effort,
    ).to("cuda")
    print(f"\n=== Reasoning effort: {effort} ===")
    _ = model.generate(**inputs, max_new_tokens=64, streamer=TextStreamer(tokenizer))'''

# -----------------------------
# 資料集處理
# -----------------------------
dataset = load_dataset("json", data_files="/home/gange/CS_Agent/modified_sharegpt_data.json", split="train")


# 將 conversation 轉成可用 text（過濾掉系統訊息，只保留用戶和助手訊息）
def formatting_prompts_func(examples):
    convos = examples["messages"]
    # 過濾掉系統訊息，只保留 "user" 和 "assistant" 角色
    filtered_convos = [[msg for msg in convo if msg.get("role") in ["user", "assistant"]] for convo in convos]
    texts = [tokenizer.apply_chat_template(convo, tokenize=False, add_generation_prompt=False) for convo in filtered_convos]
    return {"text": texts}

dataset = dataset.map(formatting_prompts_func, batched=True)

print(dataset[0]["text"])

# -----------------------------
# 建立 Trainer
# -----------------------------
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    args=SFTConfig(
        per_device_train_batch_size=4,
        gradient_accumulation_steps=4,
        warmup_steps=50,
        max_steps=500,
        learning_rate=2e-4,
        logging_steps=1,
        optim="adamw_8bit",
        weight_decay=0.001,
        lr_scheduler_type="linear",
        seed=3407,
        output_dir="outputs",
        report_to="none",
    ),
)

# 如果需要只訓練回應，取消註釋以下行
# trainer = train_on_responses_only(
#     trainer,
#     instruction_part="<|start|>user<|message|>",
#     response_part="<|start|>assistant<|channel|>final<|message|>",
# )

# -----------------------------
# 範例檢查
# -----------------------------
if len(dataset) > 100:
    sample_text = dataset[100]["text"]
    inputs = tokenizer(sample_text, return_tensors="pt", padding=True, truncation=True)
    print("Sample input_ids:", inputs["input_ids"])
    print("Sample text:", sample_text)

# -----------------------------
# 訓練模型
# -----------------------------
trainer_stats = trainer.train()

# -----------------------------
# 訓練後測試推論（添加自定義系統訊息來強調角色）
# -----------------------------
print("\n=== 訓練完成後測試 ===")
# 自定義系統訊息，強調角色（例如，專門處理手機問題的助手）
custom_system_message = "你是一個繁體中文客服，需要有耐心並使用繁體中文回答客人的問題"
test_messages = [
    {"role": "system", "content": custom_system_message},
    {"role": "user", "content": "手機進水了應該要怎麼處理"}
]
for effort in ["low", "medium", "high"]:
    inputs = tokenizer.apply_chat_template(
        test_messages,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
        reasoning_effort=effort,
    ).to("cuda")
    print(f"\n=== 訓練後推理努力度: {effort} ===")
    _ = model.generate(**inputs, max_new_tokens=64, streamer=TextStreamer(tokenizer))

# -----------------------------
# 儲存模型
# -----------------------------
model.save_pretrained("lora_model")
tokenizer.save_pretrained("lora_model")
model.save_pretrained_gguf("model", tokenizer, quantization_method="q4_k_m")
