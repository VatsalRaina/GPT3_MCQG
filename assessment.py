"""
Assessment for GPT-3 generated questions and answer options.
The first 3 QA models are used for assessment and the second 3 QA models are used for prediction.
"""

import argparse
import os
import sys

import torch
from torch.utils.data import TensorDataset, DataLoader
import numpy as np
from numpy.lib.arraysetops import unique
from scipy.special import softmax

from transformers import ElectraTokenizer, ElectraForMultipleChoice, ElectraConfig
from keras.preprocessing.sequence import pad_sequences

MAXLEN = 512

parser = argparse.ArgumentParser(description='Get all command line arguments.')
parser.add_argument('--context_path', type=str, help='Load path of contexts')
parser.add_argument('--response_path', type=str, help='Load path of question_answer')
parser.add_argument('--models_dir', type=str, help='Specify path to directory containing 6 trained QA models for assessment')
parser.add_argument('--models_complexity_dir', type=str, help='Specify path to directory containing all trained complexity models')
parser.add_argument('--batch_size', type=int, default=4, help='Specify the batch size')



def get_default_device():
    if torch.cuda.is_available():
        print("Got CUDA!")
        return torch.device('cuda')
    else:
        return torch.device('cpu')

def organise_data(questions, contexts):
    organised_data = []
    for question, context in zip(questions, contexts):
        question = question.replace("[SEP]  [SEP]", "[SEP]")
        question = question.replace(" 1. ", " ").replace(" 2. ", " ").replace(" 3. ", " ").replace(" 4. ", " ")
        question = question.replace(" A. ", " ").replace(" B. ", " ").replace(" C. ", " ").replace(" D. ", " ")
        question = question.replace(" A) ", " ").replace(" B) ", " ").replace(" C) ", " ").replace(" D) ", " ")
        question = question.replace(" a) ", " ").replace(" b) ", " ").replace(" c) ", " ").replace(" d) ", " ")
        question = question.replace(" a. ", " ").replace(" b. ", " ").replace(" c. ", " ").replace(" d. ", " ")

        first_sep_pos = question.find("[SEP]")
        question = question[first_sep_pos+6:]
        first_sep_pos = question.find("[SEP]")
        qu = question[:first_sep_pos]
        opts = []
        validSEP = True
        sep_pos = first_sep_pos
        while validSEP:
            question = question[sep_pos+6:]
            sep_pos = question.find("[SEP]")
            if sep_pos == -1:
                validSEP = False
                opt = question
            else:
                opt = question[:sep_pos]
            opts.append(opt)
            if len(opts) == 4:
                break
        curr_point = {'question': qu, 'context': context, 'options':opts}
        # print(curr_point)
        organised_data.append(curr_point)
    return organised_data

def got_four_opts(test_data):
    num_valid = 0
    for ex in test_data:
        unique_opts = []
        for opt in ex['options']:
            if opt not in unique_opts:
                unique_opts.append(opt)
        if len(unique_opts) == 4:
            num_valid += 1
    return num_valid / len(test_data)

def clean(test_data):
    """"
    Each example must have 4 answer options. So if fewer than four answer
    options are provided then the additional options are generated by 
    duplicating the final provided answer option.
    """
    clean_data = []
    for ex in test_data:
        question, context, options = ex['question'], ex['context'], ex['options']
        if len(options) !=4:
            if len(options) > 4:
                while len(options) > 4:
                    _ = options.pop()
            else:
                last_option = options[-1]
                while len(options) < 4:
                    options.append(last_option)
        curr_point = {'question': question, 'context': context, 'options':options}
        clean_data.append(curr_point)
    return clean_data
        


def get_qa_predictions(test_data, models, device, args):

    repeated_data = clean(test_data)
    test_data = repeated_data

    electra_large = "google/electra-large-discriminator"
    tokenizer = ElectraTokenizer.from_pretrained(electra_large, do_lower_case=True)

    input_ids = []
    token_type_ids = []
    count = 0
    for ex in test_data:
        question, context, options = ex['question'], ex['context'], ex['options']
        four_inp_ids = []
        four_tok_type_ids = []
        for opt in options:
            combo = context + " [SEP] " + question + " " + opt
            inp_ids = tokenizer.encode(combo)
            if len(inp_ids)>512:
                inp_ids = inp_ids[-512:]
            tok_type_ids = [0 if i<= inp_ids.index(102) else 1 for i in range(len(inp_ids))]
            four_inp_ids.append(inp_ids)
            four_tok_type_ids.append(tok_type_ids)
        four_inp_ids = pad_sequences(four_inp_ids, maxlen=MAXLEN, dtype="long", value=0, truncating="post", padding="post")
        four_tok_type_ids = pad_sequences(four_tok_type_ids, maxlen=MAXLEN, dtype="long", value=0, truncating="post", padding="post")
        input_ids.append(four_inp_ids)
        token_type_ids.append(four_tok_type_ids)

    # Create attention masks
    attention_masks = []
    for sen in input_ids:
        sen_attention_masks = []
        for opt in sen:
            att_mask = [int(token_id > 0) for token_id in opt]
            sen_attention_masks.append(att_mask)
        attention_masks.append(sen_attention_masks)
    # Convert to torch tensors
    input_ids = torch.tensor(input_ids)
    input_ids = input_ids.long().to(device)
    token_type_ids = torch.tensor(token_type_ids)
    token_type_ids = token_type_ids.long().to(device)
    attention_masks = torch.tensor(attention_masks)
    attention_masks = attention_masks.long().to(device)

    ds = TensorDataset(input_ids, token_type_ids, attention_masks)
    dl = DataLoader(ds, batch_size=args.batch_size, shuffle=False)

    logits_all_models = []
    for i, model in enumerate(models):
        print("Model:", i)
        logits = []
        count = 0
        for inp_id, tok_typ_id, att_msk in dl:
            print(count)
            count+=1
            inp_id, tok_typ_id, att_msk = inp_id.to(device), tok_typ_id.to(device), att_msk.to(device)
            with torch.no_grad():
                outputs = model(input_ids=inp_id, attention_mask=att_msk, token_type_ids=tok_typ_id)
            curr_logits = outputs[0]
            logits += curr_logits.detach().cpu().numpy().tolist()
        logits_all_models.append(logits)
    logits_all_models = np.asarray(logits_all_models)
    return logits_all_models

def expected_entropy_class(probs, epsilon=1e-10):
    """
    :param probs: array [num_models, num_examples, num_classes]
    :return: array [num_examples}
    """
    log_probs = -np.log(probs + epsilon)

    return np.mean(np.sum(probs * log_probs, axis=2), axis=0)

def get_unanswerability(all_logits):
    probs = softmax(all_logits, axis=-1)
    exe = expected_entropy_class(probs)
    return np.mean(exe)

def get_accuracy(all_logits):
    pred_logits = all_logits[3:]
    assess_logits = all_logits[:3]
    pred_ens_logits = np.mean(pred_logits, axis=0)
    assess_ens_logits = np.mean(assess_logits, axis=0)
    class_pred = np.argmax(pred_ens_logits, axis=-1)
    class_assess = np.argmax(assess_ens_logits, axis=-1)
    num_correct = 0
    for pred, assess in zip(class_pred, class_assess):
        if pred == assess:
            num_correct += 1

    return num_correct / len(class_pred)

def get_complexity_predictions(test_data, models, device, args):

    repeated_data = clean(test_data)
    test_data = repeated_data

    electra_large = "google/electra-large-discriminator"
    tokenizer = ElectraTokenizer.from_pretrained(electra_large, do_lower_case=True)

    input_ids = []
    token_type_ids = []
    attention_masks = []

    for ex in test_data:
        question, context, options = ex['question'], ex['context'], ex['options']
        combo = question + " [SEP] " + context
        for opt in options:
            combo = combo + " [SEP] " + opt
        input_encodings_dict = tokenizer(combo, truncation=True, max_length=MAXLEN, padding="max_length")
        inp_ids = input_encodings_dict['input_ids']
        inp_att_msk = input_encodings_dict['attention_mask']
        tok_type_ids = [0 if i<= inp_ids.index(102) else 1 for i in range(len(inp_ids))]
        input_ids.append(inp_ids)
        token_type_ids.append(tok_type_ids)
        attention_masks.append(inp_att_msk)

    input_ids = torch.tensor(input_ids)
    input_ids = input_ids.long().to(device)
    token_type_ids = torch.tensor(token_type_ids)
    token_type_ids = token_type_ids.long().to(device)
    attention_masks = torch.tensor(attention_masks)
    attention_masks = attention_masks.long().to(device)

    ds = TensorDataset(input_ids, token_type_ids, attention_masks)
    dl = DataLoader(ds, batch_size=args.batch_size, shuffle=False)

    logits_all_models = []
    for i, model in enumerate(models):
        print("Model:", i)
        logits = []
        count = 0
        for inp_id, tok_typ_id, att_msk in dl:
            print(count)
            count+=1
            inp_id, tok_typ_id, att_msk = inp_id.to(device), tok_typ_id.to(device), att_msk.to(device)
            with torch.no_grad():
                outputs = model(input_ids=inp_id, attention_mask=att_msk, token_type_ids=tok_typ_id)
            curr_logits = outputs[0]
            logits += curr_logits.detach().cpu().numpy().tolist()
        logits_all_models.append(logits)
    logits_all_models = np.asarray(logits_all_models)
    return logits_all_models

def get_complexity(all_logits):

    ens_preds = np.mean( softmax(all_logits, axis=-1), axis=0 )
    all_complexities = []
    for curr_preds in ens_preds:
        complexity = 0.0 * curr_preds[0] + 0.5 * curr_preds[1] + 1.0 * curr_preds[2]
        all_complexities.append(complexity)
    return np.mean( np.asarray(all_complexities), axis=0 )


def main(args):
    if not os.path.isdir('CMDs'):
        os.mkdir('CMDs')
    with open('CMDs/train.cmd', 'a') as f:
        f.write(' '.join(sys.argv) + '\n')
        f.write('--------------------------------\n')

    with open(args.response_path, 'r') as f:
        all_responses = [a.rstrip() for a in f.readlines()]

    with open(args.context_path, 'r') as f:
        all_contexts = [a.rstrip() for a in f.readlines()]

    organised_data = organise_data(all_responses, all_contexts)

    frac_four_opts = got_four_opts(organised_data)
    print("Fraction of samples with 4 unique options:", frac_four_opts)


    device = get_default_device()
    models = []
    seeds = [1, 2, 3, 4, 5, 6]
    for seed in seeds:
        model_path = args.models_dir + str(seed) + '/electra_QA_MC_seed' + str(seed) + '.pt'
        model = torch.load(model_path, map_location=device)
        model.eval().to(device)
        models.append(model)
    
    all_logits_extended = get_qa_predictions(organised_data, models, device, args)
    all_logits = all_logits_extended[:3]

    frac_unans = get_unanswerability(all_logits)
    print("Unanswerability score:", frac_unans)

    frac_acc = get_accuracy(all_logits_extended)
    print("Fraction accuracy:", frac_acc)

    complexity_models = []
    seeds = [1, 2, 3]
    for seed in seeds:
        model_path = args.models_complexity_dir + str(seed) + '/electra_seed' + str(seed) + '.pt'
        model = torch.load(model_path, map_location=device)
        model.eval().to(device)
        complexity_models.append(model)

    all_complexity_logits = get_complexity_predictions(organised_data, complexity_models, device, args)

    complexity = get_complexity(all_complexity_logits)
    print("Complexity:", complexity)


if __name__ == '__main__':
    args = parser.parse_args()
    main(args)