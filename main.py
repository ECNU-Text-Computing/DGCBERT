import argparse
import datetime
import json
import os
import random
import sys

import dgl
import numpy as np
import pandas as pd

import torch

from pretrained_models.dgc_bert import BAGIG
from pretrained_models.dgc_bert_ablation import BAGIGA, BAGIGS
from pretrained_models.BAGT import BAGT
from pretrained_models.bert import BERT
from pretrained_models.scibert import SciBERT

torch.backends.cudnn.benchmark = True

from data_processor import DataProcessor
from pretrained_models.BAG import BAG


PAD = '[PAD]'


def get_DL_data(base_config=None, train_word2vec=False, data_source='openreview', load_vocab=False,
                BERT_tokenizer_path=None, load_data=None, seed=123):
    if not BERT_tokenizer_path:
        BERT_tokenizer_path = base_config['vocab_path'] if 'vocab_path' in base_config.keys() else None
    # 读取并划分疏忽聚集
    dataProcessor = DataProcessor(pad_size=base_config['pad_size'], data_source=data_source, seed=seed)
    dataProcessor.batch_size = base_config['batch_size']
    dataProcessor.embed_dim = base_config['embed_dim']
    # dataProcessor.extract_data()
    if load_data == 'saved_data':
        dataProcessor.saved_data = base_config['saved_data']
        dataProcessor.get_saved_dataloader(base_config)
        load_vocab = True
        train_contents = None
        dataProcessor.build_vocab(train_contents, load_vocab=load_vocab, BERT_tokenizer_path=BERT_tokenizer_path)
    elif load_data == 'chunk_data':
        dataProcessor.get_chunk_dataloader(base_config)
        dataProcessor.vocab = []
    elif load_data == 'han_data':
        dataProcessor.get_han_dataloader(base_config)
    else:
        label_dict = dataProcessor.label_map()
        # label_dict = json.load(open(dataProcessor.data_cat_path + 'label_map.json', 'r'))
        dataProcessor.num_class = len(label_dict)
        dataProcessor.split_data(rate=base_config['rate'], fixed_num=base_config['fixed_num'])
        if base_config['fixed_data']:
            all_contents, all_labels, \
            train_contents, train_labels, train_indexes, \
            val_contents, val_labels, val_indexes, \
            test_contents, test_labels, test_indexes = dataProcessor.load_data()
        else:
            all_contents, all_labels, \
            train_contents, train_labels, train_indexes, \
            val_contents, val_labels, val_indexes, \
            test_contents, test_labels, test_indexes = dataProcessor.load_data(seed=dataProcessor.seed)
        num_class = len(label_dict)
        # print([label for idx, (content, label) in enumerate(train_dataloader)])
        if train_word2vec:
            dataProcessor.get_word2vec(train_contents, base_config['embed_dim'])
            dataProcessor.build_vocab(train_contents,
                                      './checkpoints/{}/{}_{}'.format(dataProcessor.data_source,
                                                                      base_config['word2vec'],
                                                                      dataProcessor.seed),
                                      load_vocab, BERT_tokenizer_path=BERT_tokenizer_path)
            dataProcessor.get_dataloader(dataProcessor.batch_size, cut=base_config['cut'])
        elif base_config['word2vec']:
            print('load_word2vec')
            dataProcessor.build_vocab(train_contents,
                                      './checkpoints/{}/{}_{}'.format(dataProcessor.data_source,
                                                                      base_config['word2vec'],
                                                                      dataProcessor.seed),
                                      load_vocab, BERT_tokenizer_path=BERT_tokenizer_path)
            dataProcessor.get_dataloader(dataProcessor.batch_size, cut=base_config['cut'])
        else:
            dataProcessor.build_vocab(train_contents, load_vocab=load_vocab, BERT_tokenizer_path=BERT_tokenizer_path)
            dataProcessor.get_dataloader(dataProcessor.batch_size, using_bert=BERT_tokenizer_path,
                                         cut=base_config['cut'])
    print(len(dataProcessor.vocab))

    return dataProcessor


def train_single(model_name='simple_model', dataProcessor=None, model_config=None, seed=None, args=None):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(model_name)
    model = get_model(model_name, dataProcessor, device, model_config, seed=seed, args=args)

    final_results = model.train_batch(dataProcessor.dataloaders, model_config['epochs'], model_config['lr'],
                                      optimizer=model_config['optimizer'], scheduler=model_config['scheduler'],
                                      record_path='./checkpoints/results/{}/'.format(dataProcessor.data_source),
                                      save_path='./checkpoints/{}/'.format(dataProcessor.data_source))
    # model.save_model('./checkpoints/{}/{}.pkl'.format(dataProcessor.data_source, model.model_name))
    # model.test(dataProcessor.dataloaders[2])
    return final_results


def train_multi(model_list, dataProcessor=None, model_configs=None, args=None):
    with open('./checkpoints/results/{}/benchmark_{}.csv'.format(dataProcessor.data_source, dataProcessor.data_source),
              'w') as fw:
        fw.write('model_name,accu_test,prec_test,recall_test,maf1_test,f1_test,auc_test,log_loss_test\n')
        for model_name in model_list:
            try:
                final_results = train_single(model_name, dataProcessor, model_configs[model_name], args)
                fw.write(
                    ','.join([model_name] + ['{:1.3f}'.format(val) for val in final_results])
                    + '\n')
            except Exception:
                print("Unexpected error:", sys.exc_info())
                print(model_name + ' fail!')
            finally:
                continue


def train_single_vote(model_name='simple_model', dataProcessor=None, model_config=None):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(model_name)
    model = get_model(model_name, dataProcessor, device, model_config)

    model.train_batch_vote(dataProcessor.dataloaders, model_config['epochs'], model_config['lr'],
                           optimizer=model_config['optimizer'], scheduler=model_config['scheduler'],
                           record_path='./checkpoints/results/{}/'.format(dataProcessor.data_source),
                           save_path='./checkpoints/{}/'.format(dataProcessor.data_source))
    model.save_model('./checkpoints/{}/{}.pkl'.format(dataProcessor.data_source, model.model_name))
    acc, prec, recall, maf1, mif1 = model.test_vote(dataProcessor.dataloaders[2])
    return acc, prec, recall, maf1, mif1

def get_model(model_name, dataProcessor, device, model_config, seed=None, args=None):
    model = None
    if args:
        args = args.__dict__
    keep_prob = model_config["keep_prob"] if "keep_prob" in model_config.keys() else 0.5
    T = model_config["T"] if "T" in model_config.keys() else 400
    if model_name == 'BERT':
        hidden_size = model_config["hidden_size"] if "hidden_size" in model_config.keys() else 768
        model = BERT(len(dataProcessor.vocab), dataProcessor.embed_dim, dataProcessor.num_class,
                     dataProcessor.vocab[PAD], dataProcessor.vectors, hidden_size=hidden_size,
                     pad_size=dataProcessor.pad_size, model_path=model_config['model_path'], keep_prob=keep_prob, T=T,
                     seed=seed, mode=model_config["mode"], args=args
                     ).to(device)
    elif model_name == 'SciBERT':
        hidden_size = model_config["hidden_size"] if "hidden_size" in model_config.keys() else 768
        model = SciBERT(len(dataProcessor.vocab), dataProcessor.embed_dim, dataProcessor.num_class,
                        dataProcessor.vocab[PAD], dataProcessor.vectors, hidden_size=hidden_size,
                        pad_size=dataProcessor.pad_size, model_path=model_config['model_path'],
                        keep_prob=keep_prob, T=T, seed=seed, mode=model_config["mode"], args=args
                        ).to(device)
    elif model_name == 'BAG':
        hidden_size = model_config["hidden_size"] if "hidden_size" in model_config.keys() else 768
        model = BAG(len(dataProcessor.vocab), dataProcessor.embed_dim, dataProcessor.num_class,
                    dataProcessor.vocab[PAD], dataProcessor.vectors, hidden_size=hidden_size, keep_prob=keep_prob,
                    pad_size=dataProcessor.pad_size, model_path=model_config['model_path'], mode=model_config["mode"],
                    model_type=model_config["model_type"], T=T, seed=seed, args=args
                    ).to(device)
    elif model_name == 'BAGT':
        hidden_size = model_config["hidden_size"] if "hidden_size" in model_config.keys() else 768
        model = BAGT(len(dataProcessor.vocab), dataProcessor.embed_dim, dataProcessor.num_class,
                     dataProcessor.vocab[PAD], dataProcessor.vectors, hidden_size=hidden_size, keep_prob=keep_prob,
                     pad_size=dataProcessor.pad_size, model_path=model_config['model_path'], mode=model_config["mode"],
                     model_type=model_config["model_type"], T=T, seed=seed, args=args
                     ).to(device)
    elif model_name == 'BAGIG':
        hidden_size = model_config["hidden_size"] if "hidden_size" in model_config.keys() else 768
        model = BAGIG(len(dataProcessor.vocab), dataProcessor.embed_dim, dataProcessor.num_class,
                      dataProcessor.vocab[PAD], dataProcessor.vectors, hidden_size=hidden_size, keep_prob=keep_prob,
                      pad_size=dataProcessor.pad_size, model_path=model_config['model_path'], mode=model_config["mode"],
                      model_type=model_config["model_type"], T=T, seed=seed, args=args
                      ).to(device)
    elif model_name == 'BAGIGA':
        hidden_size = model_config["hidden_size"] if "hidden_size" in model_config.keys() else 768
        model = BAGIGA(len(dataProcessor.vocab), dataProcessor.embed_dim, dataProcessor.num_class,
                       dataProcessor.vocab[PAD], dataProcessor.vectors, hidden_size=hidden_size, keep_prob=keep_prob,
                       pad_size=dataProcessor.pad_size, model_path=model_config['model_path'],
                       mode=model_config["mode"],
                       model_type=model_config["model_type"], ablation_module=model_config['ablation_module'], T=T,
                       seed=seed, args=args).to(device)
    elif model_name == 'BAGIGS':
        hidden_size = model_config["hidden_size"] if "hidden_size" in model_config.keys() else 768
        model = BAGIGS(len(dataProcessor.vocab), dataProcessor.embed_dim, dataProcessor.num_class,
                       dataProcessor.vocab[PAD], dataProcessor.vectors, hidden_size=hidden_size, keep_prob=keep_prob,
                       pad_size=dataProcessor.pad_size, model_path=model_config['model_path'],
                       mode=model_config["mode"],
                       model_type=model_config["model_type"], ablation_module=model_config['ablation_module'], T=T,
                       seed=seed, args=args).to(device)

    return model


def get_test_value(data_processer, best_model, model_configs):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    train_dataloader, val_dataloader, test_dataloader = dataProcessor.dataloaders
    with open('./checkpoints/results/{}/benchmark_{}_best.csv'.format(dataProcessor.data_source,
                                                                      dataProcessor.data_source),
              'w') as fw:
        fw.write('model_name,accu_test,prec_test,recall_test,maf1_test,f1_test,auc_test,log_loss_test\n')
        for model_name in best_model.keys():
            print(model_name)
            model = get_model(model_name, dataProcessor, device, model_configs[model_name])
            model = model.load_model(
                './checkpoints/{}/{}_{}.pkl'.format(data_processer.data_source, model_name, best_model[model_name]))
            final_results = model.test(test_dataloader)
            fw.write(
                ','.join([model_name] + ['{:1.3f}'.format(val) for val in final_results])
                + '\n')


def get_best_model(data_source, model_list, key):
    result_path = './checkpoints/results/{}/'.format(data_source)
    best_dict = {}
    # selected_columns = ['model'] + 'epoch,loss,accu_train,auc_train,log_loss_train,accu_val,prec_val,recall_val,' \
    #                                'f1_val,auc_val,log_loss_val,accu_test,prec_test,recall_test,f1_test,auc_test,' \
    #                                'log_loss_test'.split(',')
    columns = ['model'] + \
              'epoch,' \
              'loss_train,accu_train,roc_auc_train,log_loss_train,prec_train,recall_train,f1_train,pr_auc_train,' \
              'prec_neg_train,recall_neg_train,f1_neg_train,pr_auc_neg_train,' \
              'loss_val,accu_val,roc_auc_val,log_loss_val,prec_val,recall_val,f1_val,pr_auc_val,' \
              'prec_neg_val,recall_neg_val,f1_neg_val,pr_auc_neg_val,' \
              'loss_test,accu_test,roc_auc_test,log_loss_test,prec_test,recall_test,f1_test,pr_auc_test,' \
              'prec_neg_test,recall_neg_test,f1_neg_test,pr_auc_neg_test'.split(',')
    result_df = pd.DataFrame(columns=columns)
    print(len(model_list))
    for model in model_list:
        print(model)
        df = pd.read_csv(result_path + '{}_records.csv'.format(model))
        # print(df)
        # print(df.columns)
        max_value = df[key].max()
        # print(max_value)
        max_data = df[df[key] == max_value]
        max_data['model'] = [model] * len(max_data)
        result_df = result_df.append(max_data.tail(1)[columns])
        # max_epoch = max_data.index.tolist()
        # best_dict[model] = max_epoch[-1] + 1
    result_df.to_csv('./checkpoints/results/{}/best_results_{}.csv'.format(data_source, data_source))
    return best_dict


def get_configs(data_source, model_list):
    fr = open('./configs/{}.json'.format(data_source))
    configs = json.load(fr)
    full_configs = {'default': configs['default']}
    for model in model_list:
        full_configs[model] = configs['default'].copy()
        if model in configs.keys():
            for key in configs[model].keys():
                full_configs[model][key] = configs[model][key]
    return full_configs


def get_pretrained_test(data_source, model_name, model_config, key):
    dataProcessor = get_DL_data(base_config=model_config, data_source=data_source,
                                BERT_tokenizer_path=model_config['vocab_path'])
    df = pd.read_csv('./checkpoints/results/{}/{}_records.csv'.format(data_source, model_name))
    max_value = df[key].max()
    max_epoch = df[df[key] == max_value].index.tolist()
    best_model = max_epoch[-1] + 1
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    train_dataloader, val_dataloader, test_dataloader = dataProcessor.dataloaders
    model = get_model(model_name, dataProcessor, device, model_config)
    model = model.load_model(
        './checkpoints/{}/{}_{}.pkl'.format(dataProcessor.data_source, model_name, best_model))
    acc, prec, recall, maf1, f1 = model.test(test_dataloader)


def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)

    dgl.seed(seed)
    # torch.backends.cudnn.deterministic = True
    os.environ["OMP_NUM_THREADS"] = '1'


class Dict(dict):
    __setattr__ = dict.__setitem__
    __getattr__ = dict.__getitem__


def dictToObj(dictObj):
    if not isinstance(dictObj, dict):
        return dictObj
    d = Dict()
    for k, v in dictObj.items():
        d[k] = dictToObj(v)
    return d


def phase_model_train(phase, data_source, model_name, model_config, seed, mode=None, args=None):
    final_results = None
    if phase == 'model':
        dataProcessor = get_DL_data(base_config=model_config, data_source=data_source,
                                    load_data=model_config['saved_data'], seed=seed)
        final_results = train_single(model_name=model_name, dataProcessor=dataProcessor, model_config=model_config,
                                     args=args)
    elif phase == 'pretrained':
        model_config['mode'] = mode
        print(model_config)
        dataProcessor = get_DL_data(base_config=model_config, data_source=data_source,
                                    BERT_tokenizer_path=model_config['vocab_path'],
                                    load_data=model_config['saved_data'], seed=seed)
        final_results = train_single(model_name=model_name, dataProcessor=dataProcessor, model_config=model_config,
                                     seed=seed, args=args)

    return final_results


def get_mistake_results(model_path, model_name, model_config, phase, data_source, seed, args):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(model_path)
    if phase == 'model':
        dataProcessor = get_DL_data(base_config=model_config, data_source=data_source,
                                    load_data=model_config['saved_data'], seed=seed)
    elif phase == 'pretrained':
        dataProcessor = get_DL_data(base_config=model_config, data_source=data_source,
                                    BERT_tokenizer_path=model_config['vocab_path'],
                                    load_data=model_config['saved_data'], seed=seed)
    else:
        print('no such phase')
        raise Exception
    # model = get_model(model_name, dataProcessor, device, model_config, seed, args)
    # model = model.load_model(model_path)
    model = torch.load(model_path)
    print(model)
    mistake_results = model.get_mistake_results(dataProcessor.dataloaders[2])
    # torch.save(mistake_results, './checkpoints/results/{}/mistake_results_{}.list'.format(data_source, model_name))
    with open('./data/{}/content.list'.format(data_source)) as fr:
        contents = fr.readlines()
    probits, labels, true_labels, indexes, states = zip(*mistake_results)
    text_data = []
    for index in indexes:
        text_data.append(contents[index].strip())
    df = pd.DataFrame()
    df['index'] = indexes
    df['content'] = text_data
    df['probit'] = probits
    df['label'] = labels
    df['true_label'] = true_labels
    df.to_csv('./checkpoints/results/{}/mistake_results_{}.csv'.format(data_source, model_name))


if __name__ == '__main__':
    start_time = datetime.datetime.now()
    parser = argparse.ArgumentParser(description='Process some description.')

    parser.add_argument('--phase', default='best_model', help='the function name.')
    parser.add_argument('--ablation', default=None, help='the ablation modules.')
    parser.add_argument('--data_source', default='PeerRead', help='the data source.')
    parser.add_argument('--mode', default=None, help='the model mode.')
    parser.add_argument('--type', default='BERT', help='the model type.')
    parser.add_argument('--seed', default=555, help='the data seed.')
    parser.add_argument('--model_seed', default=123, help='the model seed.')
    parser.add_argument('--model', default=None, help='the selected model for other methods.')
    parser.add_argument('--model_path', default=None, help='the selected model for deep analysis.')
    parser.add_argument('--k', default=None, help='the layers.')
    parser.add_argument('--alpha', default=None, help='the alpha.')
    parser.add_argument('--top_rate', default=None, help='the alpha.')
    parser.add_argument('--predict_dim', default=None, help='the predict dim.')

    args = parser.parse_args()
    print('args', args)
    print('data_seed', args.seed)
    # setup_seed(int(args.seed))  # 原本的模型种子和数据种子一同修改
    MODEL_SEED = int(args.model_seed)
    setup_seed(MODEL_SEED)  # 模型种子固定为123，数据种子根据输入修改
    print('model_seed', MODEL_SEED)
    print('===============注意这里修改了模型种子！==========================')
    pretrained_models = ['BERT', 'SciBERT', 'BAG', 'BAGT', 'BAGIG', 'BAGIGA',
                         'BAGIGS']
    all_list = pretrained_models
    DATA_SOURCE = args.data_source  # ['openreview', 'AAPR', 'openreview_abstract']
    CONFIG_DICT = get_configs(DATA_SOURCE, all_list)
    DEFAULT_CONFIG = CONFIG_DICT['default']
    print(DEFAULT_CONFIG)
    PRETRAINED_MODEL = 'SciBERT'
    pretrained_types = json.load(open('./configs/pretrained_types.json', 'r'))
    for pretrained_model in pretrained_models:
        if args.type:
            model_type = args.type
            CONFIG_DICT[pretrained_model]['model_type'] = args.type
        elif CONFIG_DICT[pretrained_model]['model_type'] in pretrained_types:
            model_type = CONFIG_DICT[pretrained_model]['model_type']
        else:
            print('error! no this type!')
            model_type = None
            raise Exception
        CONFIG_DICT[pretrained_model]['model_path'] = pretrained_types[model_type]['model_path']
        CONFIG_DICT[pretrained_model]['vocab_path'] = pretrained_types[model_type]['vocab_path']

    # ablation_list = []
    # os.environ['CUDA_LAUNCH_BLOCKING'] = '1'
    if args.phase == 'test':
        print('This is a test process.')
        print(CONFIG_DICT['BERTAttentionCNN'])
        dataProcessor = get_DL_data(base_config=DEFAULT_CONFIG, data_source='PeerRead',
                                    load_data=DEFAULT_CONFIG['saved_data'], seed=123)
    elif args.phase in pretrained_models:
        if args.ablation:
            print('>>ablation start!<<')
            CONFIG_DICT[args.phase]['ablation_module'] = args.ablation
        phase_model_train('pretrained', DATA_SOURCE, args.phase, CONFIG_DICT[args.phase], args.seed, args.mode, args)
    elif args.phase == 'best_pretrained':
        get_pretrained_test(DATA_SOURCE, PRETRAINED_MODEL, CONFIG_DICT[PRETRAINED_MODEL], DEFAULT_CONFIG['best_value'])
    elif args.phase == 'mistake_results':
        model_config = CONFIG_DICT[args.model]
        if args.model in pretrained_models:
            model_type = 'pretrained'
            model_config['mode'] = None
        else:
            model_type = None
        get_mistake_results(args.model_path, args.model, model_config, model_type, args.data_source, args.seed, args)
    else:
        print('error! No such method!')
    end_time = datetime.datetime.now()
    print('{} takes {} seconds'.format(args.phase, (end_time - start_time).seconds))

    print('Done main!')
