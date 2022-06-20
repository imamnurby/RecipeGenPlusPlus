from transformers import (RobertaConfig, RobertaModel, RobertaTokenizer, RobertaForCausalLM, EncoderDecoderModel)

from models import CustomEncoderDecoderModel
from data_collator import DataCollatorForSeq2Seq
from trainer import CustomTrainer, CustomTrainingArguments

from tqdm import tqdm
# tqdm.pandas()
from typing import Optional, Any, Union, List, Dict, Tuple
from datasets import Dataset, DatasetDict, load_metric
import torch
import numpy as np
import pandas as pd
import random
import os
import copy
import json

### EXPERIMENT VARIABLE
DECODER_CLASSES = {'roberta-base': (RobertaForCausalLM, RobertaConfig)}
DATASET_PATH = "dataset-original"

# specify pretrained model
MODEL = "roberta"
assert(MODEL in ('roberta', 'codebert'))

# specify training data
EXPERIMENT = "merged-prefix-ch-fc-field-interactive"
assert(EXPERIMENT in ('merged-prefix-ch-fc-field', 'merged-prefix-ch-fc-field-interactive'))

OUTPUT_DIR = "models/rob2rand_merged_w_prefix_interactive_5-6-2022"

LOAD_FROM_CKPT = False
if LOAD_FROM_CKPT:
    ckpt = None
    # assert(os.path.exists(ckpt) == True)

DEBUG = None
DATA_NUM = 128 if DEBUG else None
NUM_BEAMS = 3
RETURN_TOP_K = 1

# setting for the tokenizer
MAX_INPUT_LENGTH = 150 
MAX_TARGET_LENGTH = 150

args = CustomTrainingArguments(
    f"{OUTPUT_DIR}",
    evaluation_strategy="epoch",
    save_strategy="epoch",
    # save_steps=1000 if not DEBUG else 1,
    # logging_steps=1000 if not DEBUG else 1,
    do_eval=True,
    do_train=True,
    learning_rate=5e-6,
    per_device_train_batch_size=64,
    per_device_eval_batch_size=64,
    weight_decay=0.0,
    warmup_steps=1000,
    save_total_limit=3,
    num_train_epochs=50 if not DEBUG else 3,
    predict_with_generate=True,
    fp16=True,
    optim='adamw_torch',
    generation_num_beams=NUM_BEAMS if NUM_BEAMS else None,
    generation_max_length=MAX_TARGET_LENGTH,
    num_return_sequences=RETURN_TOP_K,
    metrics_to_check=[('eval_bleu_em', True)])
###

### LOAD DATASET
def helper_prepare_data(x, mode):
    temp_list = x.split("<sep>")
    temp_list = [item.strip() for item in temp_list]
    
    if mode=="ac":
        temp_list = temp_list[:2].copy()
    elif mode=="af":
        temp_list = temp_list[:3].copy()
    
    return " ".join(temp_list)

def get_dataset_path(root=DATASET_PATH, exp=EXPERIMENT):
    prefix_tc="GENERATE TRIGGER CHANNEL <pf> "
    prefix_tf="GENERATE TRIGGER FUNCTION <pf> "
    prefix_ac="GENERATE ACTION CHANNEL <pf> "
    prefix_af="GENERATE ACTION FUNCTION <pf> "
    
    if exp=="merged-prefix-ch-fc-field-interactive":
        datapath = os.path.join(root, "processed.csv")
        df = pd.read_csv(datapath)
        df = df[df.granularity=="function"].copy()
        
        function_tc = df.copy()
        function_tc['target'] = function_tc.target.apply(lambda x: x.split("<sep>")[0].strip())
        function_tc['source'] = function_tc.source.apply(lambda x: prefix_tc + x)
        
        function_tf = df.copy()
        function_tf['temp'] = function_tf.target.apply(lambda x: x.split("<sep>")[0].strip())
        function_tf['target'] = function_tf.target.apply(lambda x: x.split("<sep>")[1].strip())
        function_tf['source'] = function_tf.apply(lambda x: prefix_tf + x.source + " <out> " + x.temp, axis=1)
        function_tf.drop(columns=["temp"], inplace=True)
        
        function_ac = df.copy()
        function_ac['temp'] = function_ac.target.apply(lambda x: helper_prepare_data(x, mode="ac"))
        function_ac['target'] = function_ac.target.apply(lambda x: x.split("<sep>")[2].strip())
        function_ac['source'] = function_ac.apply(lambda x: prefix_ac + x.source + " <out> " + x.temp, axis=1)
        function_ac.drop(columns=["temp"], inplace=True)
        
        function_af = df.copy()
        function_af['temp'] = function_af.target.apply(lambda x: helper_prepare_data(x, mode="af"))
        function_af['target'] = function_af.target.apply(lambda x: x.split("<sep>")[3].strip())
        function_af['source'] = function_af.apply(lambda x: prefix_af + x.source + " <out> " + x.temp, axis=1)
        function_af.drop(columns=["temp"], inplace=True)
        
        df = pd.concat([function_tc, function_tf, function_ac, function_af])
        df.drop(columns="granularity", inplace=True)
        
        df_dict={'train': df[df.split=='train'].copy(),
                'val': df[df.split=='val'].copy(), 
                'gold': df[df.split=='gold'].copy(),
                'noisy': df[df.split=='noisy'].copy()}
    return df_dict


def load_dataset(path_dict, number=None):
    assert(type(path_dict)==dict)
    df_dict = {}
    for split, path in path_dict.items():
        if number:
            df_dict[split] = pd.read_pickle(path).sample(n=number, random_state=1234).copy()
        else:
            df_dict[split] = pd.read_pickle(path)
    return df_dict

def convert_to_dataset(df_dict):
    train = Dataset.from_pandas(df_dict['train']).remove_columns(['__index_level_0__', 'split'])
    val = Dataset.from_pandas(df_dict['val']).remove_columns(['__index_level_0__', 'split'])
    gold = Dataset.from_pandas(df_dict['gold']).remove_columns(['__index_level_0__', 'split'])
    noisy = Dataset.from_pandas(df_dict['noisy']).remove_columns(['__index_level_0__', 'split'])
    
    return DatasetDict({'train':train,
                        'val':val,
                        'gold':gold,
                        'noisy':noisy})
###

### LOAD TOKENIZER
def load_tokenizer(model=MODEL):
    if model == 'roberta':
        tokenizer = RobertaTokenizer.from_pretrained("roberta-base")
    elif model == 'codebert':
        tokenizer = RobertaTokenizer.from_pretrained("microsoft/codebert-base")
    else:
        raise ValueError(f"Undefined model type")
    return tokenizer
###

### PREPROCESS DATASET
def preprocess_function(examples):
    inputs = [ex for ex in examples["source"]]
    targets = [ex for ex in examples["target"]]
    model_inputs = tokenizer(inputs, max_length=MAX_INPUT_LENGTH, truncation=True, padding=False)
    
    with tokenizer.as_target_tokenizer():
        labels = tokenizer(targets, max_length=MAX_TARGET_LENGTH, truncation=True, padding=False)

    model_inputs["labels"] = labels["input_ids"]
    return model_inputs
###

### METRICS
bleu = load_metric("sacrebleu")
em = load_metric("exact_match")

def compute_metrics(eval_preds):
    
    def decode_preds(eval_preds):
        preds, labels = eval_preds
        # In case the model returns more than the prediction logits
        if isinstance(preds, tuple):
            preds = preds[0]

        decoded_preds = tokenizer.batch_decode(preds, skip_special_tokens=True)

        # Replace -100s in the labels as we can't decode them
        labels = np.where(labels != -100, labels, tokenizer.pad_token_id)
        decoded_labels = tokenizer.batch_decode(labels, skip_special_tokens=True)

        # Some simple post-processing
        decoded_preds = [pred.split("<pf>")[-1].strip() for pred in decoded_preds]
        decoded_labels = [[label.split("<pf>")[-1].strip()] for label in decoded_labels]
        return decoded_preds, decoded_labels
    
    decoded_preds, decoded_labels = decode_preds(eval_preds)
    
    bleu_dict = bleu.compute(predictions=decoded_preds, references=decoded_labels)
    
    # decoded_preds = [pred[0] for pred in decoded_preds]
    decoded_labels = [label[0] for label in decoded_labels]
    em_dict = em.compute(predictions=decoded_preds, references=decoded_labels)
    return {"bleu": bleu_dict["score"],
           "em": em_dict['exact_match'],
           "bleu_em": (bleu_dict['score']+em_dict['exact_match'])/2}
###

if __name__ == "__main__":
    df_dict = get_dataset_path()

    dataset = convert_to_dataset(df_dict=df_dict)
    tokenizer = load_tokenizer()

    tokenized_datasets = dataset.map(
        preprocess_function,
        batched=True,
        remove_columns=dataset["train"].column_names,
    )

    if LOAD_FROM_CKPT:
        model = EncoderDecoderModel.from_pretrained(ckpt)
        print(f"Loading from {ckpt}")
    else:
        model = CustomEncoderDecoderModel.from_encoder_decoder_pretrained("roberta-base", "roberta-base", random_decoder=True, model_dict=DECODER_CLASSES)
        print("Loading not from checkpoint")
    model.config.decoder_start_token_id = tokenizer.cls_token_id
    model.config.pad_token_id = tokenizer.pad_token_id
    model.config.vocab_size = model.config.decoder.vocab_size
    model.config.architectures = "EncoderDecoderModel"
    model.config.max_length = 150

    data_collator = DataCollatorForSeq2Seq(tokenizer, model=model)

    trainer = CustomTrainer(
        model,
        args,
        train_dataset=tokenized_datasets["train"],
        eval_dataset=tokenized_datasets["val"],
        data_collator=data_collator,
        tokenizer=tokenizer,
        compute_metrics=compute_metrics
    )

    trainer.train()

