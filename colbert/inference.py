import pandas as pd
import torch
import json

from datasets import (
    Dataset,
    DatasetDict,
    Features,
    Sequence,
    Value,
    load_from_disk,
    load_metric,
)
from transformers import (
    AutoConfig,
    AutoTokenizer,
)
from .tokenizer import *
from .model import *

# baseline : https://github.com/boostcampaitech3/level2-mrc-level2-nlp-11


def run_colbert_retrieval(datasets, model_args, training_args, load_rank_path=None, top_k=10):
    test_dataset = datasets["train"].flatten_indices().to_pandas()
    MODEL_NAME = "klue/bert-base"

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    model_config = AutoConfig.from_pretrained(MODEL_NAME)
    special_tokens = {"additional_special_tokens": ["[Q]", "[D]"]}
    ret_tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    ret_tokenizer.add_special_tokens(special_tokens)
    model = ColbertModel.from_pretrained(MODEL_NAME)
    model.resize_token_embeddings(ret_tokenizer.vocab_size + 2)

    model.to(device)

    model.load_state_dict(torch.load(model_args.retrieval_ColBERT_path))

    print("opening wiki passage...")
    with open("./data/wikipedia_documents.json", "r", encoding="utf-8") as f:
        wiki = json.load(f)
    context = list(dict.fromkeys([v["text"] for v in wiki.values()]))
    print("wiki loaded!!!")

    query = list(test_dataset["question"])

    mrc_ids = test_dataset["id"]
    length = len(test_dataset)

    if load_rank_path:
        print("retriever : load rank")
        rank = torch.load(load_rank_path)
    else:
        batched_p_embs = []
        with torch.no_grad():
            model.eval

            q_seqs_val = tokenize_colbert(query, ret_tokenizer, corpus="query").to("cuda")
            q_emb = model.query(**q_seqs_val).to("cpu")
            print(q_emb.size())

            print("Start passage embedding.. ....")
            p_embs = []
            for step, p in enumerate(tqdm(context)):
                p = tokenize_colbert(p, ret_tokenizer, corpus="doc").to("cuda")
                p_emb = model.doc(**p).to("cpu").numpy()
                p_embs.append(p_emb)
                if (step + 1) % 200 == 0:
                    batched_p_embs.append(p_embs)
                    p_embs = []
            batched_p_embs.append(p_embs)

        dot_prod_scores = model.get_score(q_emb, batched_p_embs, eval=True)
        print(dot_prod_scores.size())

        rank = torch.argsort(dot_prod_scores, dim=1, descending=True).squeeze()
        print(dot_prod_scores)
        print(rank)
        torch.save(rank, "colbert/retriever_infer/inferecne_colbert_rank.pth")
    print(rank.size())
    print(length)

    passages = []

    for idx in range(length):
        passage = ""
        for i in range(top_k):
            passage += context[rank[idx][i]]
            passage += "$%$"
        passages.append(passage)

    # test data 에 대해선 정답이 없으므로 id question context 로만 데이터셋이 구성됩니다.
    if training_args.do_predict:
        f = Features(
            {
                "context": Value(dtype="string", id=None),
                "id": Value(dtype="string", id=None),
                "question": Value(dtype="string", id=None),
            }
        )
        df = pd.DataFrame({"question": query, "id": mrc_ids, "context": passages})

    # train data 에 대해선 정답이 존재하므로 id question context answer 로 데이터셋이 구성됩니다.
    elif training_args.do_eval:
        f = Features(
            {
                "answers": Sequence(
                    feature={
                        "text": Value(dtype="string", id=None),
                        "answer_start": Value(dtype="int32", id=None),
                    },
                    length=-1,
                    id=None,
                ),
                "context": Value(dtype="string", id=None),
                "id": Value(dtype="string", id=None),
                "question": Value(dtype="string", id=None),
            }
        )
        df = pd.DataFrame(
            {
                "question": query,
                "id": mrc_ids,
                "context": passages,
                "answers": test_dataset["answers"],
            }
        )
    else:
        raise ValueError
    df.to_csv("colbert_rank")
    complete_datasets = DatasetDict({"validation": Dataset.from_pandas(df, features=f)})
    return complete_datasets