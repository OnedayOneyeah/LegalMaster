# import modules
import argparse
import logging
import random
import pickle
import ray
import tqdm
import shortuuid
import pandas as pd
import numpy as np

## dataset
from dataset import *

## models
import torch
import torch.nn as nn
import transformers
from transformers import LlamaForCausalLM, LlamaTokenizer
from peft import PeftModel

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# pipeline
def make_dataset(data_dir):
    # build dataset
    path = data_dir

    # load file
    if os.path.isfile(os.path.join(path, 'prompt.pkl')):
        with open(os.path.join(path, 'prompt.pkl'), 'rb') as f:
            dataset = pickle.load(f)
    else:
        dataset_list = build_dataset(['case_hold'], path)
        dataset = dataset_list[0]['test']
        random.seed(7)
        dataset = dataset.shuffle().map(prompt_engineering).select_columns(['question', 'label'])
        dataset = dataset.add_column(name = 'idx', column = range(3600))
        # save file
        with open(os.path.join(path, 'prompt.pkl'), 'wb') as f:
            pickle.dump(dataset, f)

    return dataset

def prompt_engineering(data_point):

    prompt_cands = [
    "Please select the most suitable summary of the legal ruling that accompanies the relevant referenced decisions for the specific case. The following excerpt is from the court's decision.",
    "Kindly choose the concise summary of the legal ruling that accompanies the relevant referenced decisions applicable to the given case. Provided below is an excerpt from the court decision.",
    "Please decide on the most appropriate summary of the legal ruling that accompanies the relevant referenced decisions, which are relevant to the given case. Here is an excerpt from the court decision for your consideration.",
    "Here is an excerpt from the court decision for the case. Please choose the most appropriate short summary of the legal ruling that accompanies the referenced decisions relevant to the case.",
    "Consider the following excerpt from the court decision for the case. Your task is to select the most appropriate short summary of the legal ruling that accompanies the referenced decisions relevant to the case.",
    "Given the excerpt from the court decision for the case, your task is to choose the most appropriate short summary of the legal ruling that accompanies the referenced decisions relevant to the case.",
    "Please refer to the following excerpt from the court decision for the case. Your task is to choose the most appropriate short summary of the legal ruling that accompanies the referenced decisions relevant to the case.",
    "Your task is to choose the most appropriate short summary of the legal ruling that accompanies the referenced decisions relevant to the case, using the excerpt from the court decision provided below.",
    "Please review the following excerpt from the court decision for the case. Your task is to choose the most appropriate short summary of the legal ruling that accompanies the referenced decisions relevant to the case.",
    "Here is the excerpt from the court decision for the case. Your task is to choose the most appropriate short summary of the legal ruling that accompanies the referenced decisions relevant to the case."
    ] # generated by Chat-GPT

    
    _slice = data_point['context'].find('(<HOLDING>)')
    excerpt = data_point['context'][:_slice]
    choices = [str(n) + f': {c}' for n,c in enumerate(data_point['endings'])]
    prompt = random.choice(prompt_cands)
    input = prompt + f'\nExcerpt: {excerpt}' + f'\nChoices: {choices}'

    return {
        'question' : input,
        'label' : data_point['label']
    }

def run_eval(model_id, dataset, answer_path, num_gpus = 3):
    dataset = make_dataset('./dataset')
    questions = dataset.select_columns(['question', 'idx'])

    chunk_size = len(questions) // num_gpus
    ans_handlers = []

    for i in range(0, len(questions), chunk_size):
        ans_handlers.append(
            get_model_answers.remote(
                model_id, questions[i:i+chunk_size]
            )
        )
    answers = []

    for ans_handler in ans_handlers:
        answers.extend(ray.get(ans_handler))
    answers = datasets.Dataset.from_pandas(pd.DataFrame(answers)) # convert to Dataset obj

    # save the answer
    with open(os.path.join(answer_path, 'answers.pkl'), 'wb') as f:
        pickle.dump(answers, f)

@ray.remote(num_gpus = 3)
@torch.inference_mode()
def get_model_answers(model_id, questions):
    tokenizer = LlamaTokenizer.from_pretrained(
        './llama',
        add_eos_token = True
    )
    tokenizer.pad_token_id = 0 # we will pad the sequence til the max length

    model = LlamaForCausalLM.from_pretrained(
        './llama',
        device_map = 'auto'
    ).cuda()

    answers = []

    for i, question in enumerate(tqdm(questions)):
        prompt = question['question']
        input_ids = tokenizer([prompt]).input_ids # [[]]
        output_ids = model.generate(
            torch.as_tensor(input_ids).cuda(),
            temperature = 0.7, # manipulates how strict the model will follow the prompt instruction
            max_new_tokens = 1024,
        )
        output_ids = output_ids[0][len(input_ids[0]) : ] # only take the answer, not the question prompt from the outputs
        outputs = tokenizer.decode(output_ids, skip_special_tokens = True).strip()
        ans_id = shortuuid.uuid()
        answers.append(
            {
                "idx" : question['idx'],
                "answer" : outputs,
                "answer_id" : ans_id,
                "model_id" : model_id

            }
        )

        return answers


def evaluate(model_id, data_dir, answer_path, num_gpus):
    # define model
    model_name = ['llama_legal', 'llama_chat', 'llama_legal_chat', 'llama_chat_legal'][model_id]

    # get answers
    ray.init()
    dataset = make_dataset(data_dir)
    run_eval(model_id, dataset, answer_path, 3)

    with open(os.path.join(answer_path, './answers.pkl', 'rb')) as f:
        answers = pickle.load(f).select_columns(['answer', 'idx'])
    labels = dataset.select_columns(['label', 'idx'])

    dataset = datasets.concatenate_datasets([answers, labels])

    # calculate metric
    calmet_handlers = []
    chunk_size = len(dataset) // num_gpus

    for i in range(0, len(dataset), chunk_size):
        calmet_handlers.append(
            calculate_metric.remote(dataset[i:i+chunk_size])
            )
    
    results = [] 
    
    for calmet_handler in calmet_handlers:
        results.append(ray.get(calmet_handler)) # [[0,0,0,0,0,0,0],[0,0,0,0,0,0,0], ...]
    
    metrics = results.sum(axis = 0)
    accuracy = metrics[1] / (results[0] + results[1])
    
    print(f'The accuracy of the {model_name} is : {accuracy}')

@ray.remote(gpu_nums = 3)
def calculate_metric(dataset):

    for i, data in enumerate(dataset):

        idx = dataset['idx']
        answer = dataset['answer']
        label = dataset['label']

        incorrect_correct_labels = np.array([0,0,0,0,0,0,0]) # one-hot-encoding: [incorrect, correct, 0,1,2,3,4 (ground truth)]

        r = int(label in answer) # incorrect: 0, correct: 1
        incorrect_correct_labels[r] += 1
        incorrect_correct_labels[label+2] += 1
    
    return incorrect_correct_labels
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_dir',
                        type = str,
                        default = './dataset',
                        help = 'Where LexGlue dataset is stored')
    parser.add_argument('--answer_dir',
                        type = str,
                        default = './answers',
                        help = 'Where answers from the model is stored')
    parser.add_argument('--gpu_num',
                        type = int,
                        default = 2,
                        help = 'The number of gpus to use for evaluation')
    parser.add_argument('--model_id',
                        type = int,
                        default = 0, 
                        help = '0: llama_legal, 1: llama_chat, 2: llama_legal_chat, 3: llama_chat_legal')

    args = parser.parse_args()

    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.DEBUG)

    evaluate(args.model_id, args.data_dir, args.answer_dir, args.gpu_num)


    