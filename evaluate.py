import argparse
import os
import sys
import torch
import torch.nn as nn
from torch.backends import cudnn
from sklearn.neighbors import KDTree
from tqdm import tqdm
import util.initPara as para
from loading_pointclouds import *
import util.PointNetVlad as PNV
import config as cfg

cudnn.enabled = True

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

recall_num = 25

cfg.EVAL_DATABASE_FILE = 'generating_queries/oxford_evaluation_database.pickle'
cfg.EVAL_QUERY_FILE = 'generating_queries/oxford_evaluation_query.pickle'
DATABASE_SETS = get_sets_dict(cfg.EVAL_DATABASE_FILE)
QUERY_SETS = get_sets_dict(cfg.EVAL_QUERY_FILE)

LOG_FOUT = open(os.path.join(para.args.log_dir, 'log_train.txt'), 'w')
LOG_FOUT.write(str(para.args) + '\n')
LOG_FOUT.flush()
TOTAL_ITERATIONS = 0


def log_string(out_str):
    LOG_FOUT.write(out_str + '\n')
    LOG_FOUT.flush()
    print(out_str)

def evaluate_model(model, save_flag=True):
    # 计算 Recall @N
    recall = np.zeros(recall_num)
    count = 0
    similarity = []
    one_percent_recall = []

    DATABASE_VECTORS = []
    QUERY_VECTORS = []

    torch.cuda.empty_cache()
    if save_flag:
        fun_tqdm = tqdm
    else:
        fun_tqdm = list

    # 总共23个子图
    # 获得每个子地图的每一帧点云的描述子
    for i in fun_tqdm(range(len(DATABASE_SETS))):
        DATABASE_VECTORS.append(get_latent_vectors(model, DATABASE_SETS[i]))

    # 获得每个子地图的每一帧要被评估的点云的描述子
    for j in fun_tqdm(range(len(QUERY_SETS))):
        QUERY_VECTORS.append(get_latent_vectors(model, QUERY_SETS[j]))

    torch.cuda.empty_cache()
    for m in fun_tqdm(range(len(QUERY_SETS))):
        for n in range(len(QUERY_SETS)):
            if (m == n):
                continue
            # 寻找当前第m个子图和第n个子图的检索结果
            pair_recall, pair_similarity, pair_opr = get_recall(
                m, n, DATABASE_VECTORS, QUERY_VECTORS, QUERY_SETS)
            recall += np.array(pair_recall)
            count += 1
            one_percent_recall.append(pair_opr)
            for x in pair_similarity:
                similarity.append(x)

    ave_recall = recall / count
    if save_flag:
        print("average_similarity_score",ave_recall)

    # print(similarity)
    average_similarity_score = np.mean(similarity)
    if save_flag:
        print("average_similarity_score",average_similarity_score)
    #
    ave_one_percent_recall = np.mean(one_percent_recall)
    if save_flag:
        print("ave_one_percent_recall",ave_one_percent_recall)

    if save_flag:
        cfg.OUTPUT_FILE = os.path.join(cfg.RESULTS_FOLDER, "result.txt")
        with open(cfg.OUTPUT_FILE, "w") as output:
            output.write("Average Recall @N:\n")
            output.write(str(ave_recall))
            output.write("\n\n")
            output.write("Average top1 Similarity:\n")
            output.write(str(average_similarity_score))
            output.write("\n\n")
            output.write("Average Top 1 percent Recall:\n")
            output.write(str(ave_one_percent_recall))
            output.close()
    return ave_one_percent_recall


def get_latent_vectors(model, dict_to_process):
    model.eval()
    torch.cuda.empty_cache()
    train_file_idxs = np.arange(0, len(dict_to_process.keys()))

    batch_num = para.args.eval_batch_size * \
        (1 + para.args.positives_per_query + para.args.negatives_per_query)
    q_output = []
    for q_index in range(len(train_file_idxs)//batch_num):
        file_indices = train_file_idxs[q_index *
                                       batch_num:(q_index+1)*(batch_num)]
        file_names = []
        for index in file_indices:
            file_names.append(dict_to_process[index]["query"])
        start = time()
        queries = load_pc_files(file_names)
        # print("load time: ", time() - start)
        start = time()
        with torch.no_grad():
            feed_tensor = torch.from_numpy(queries).float()
            feed_tensor = feed_tensor.unsqueeze(1)
            feed_tensor = feed_tensor.to(device)
            print(feed_tensor.mean(dim=[0, 1, 2]))
            out = model(feed_tensor)
        # print("forward time: ", time() - start)

        out = out.detach().cpu().numpy()
        out = np.squeeze(out)
        # del feed_tensor
        #out = np.vstack((o1, o2, o3, o4))
        q_output.append(out)

    q_output = np.array(q_output)
    if(len(q_output) != 0):
        q_output = q_output.reshape(-1, q_output.shape[-1])

    # handle edge case
    index_edge = len(train_file_idxs) // batch_num * batch_num
    if index_edge < len(dict_to_process.keys()):
        file_indices = train_file_idxs[index_edge:len(dict_to_process.keys())]
        file_names = []
        for index in file_indices:
            file_names.append(dict_to_process[index]["query"])
        queries = load_pc_files(file_names)

        with torch.no_grad():
            feed_tensor = torch.from_numpy(queries).float()
            feed_tensor = feed_tensor.unsqueeze(1)
            feed_tensor = feed_tensor.to(device)
            o1 = model(feed_tensor)

        # del feed_tensor
        output = o1.detach().cpu().numpy()
        output = np.squeeze(output)
        if (q_output.shape[0] != 0):
            q_output = np.vstack((q_output, output))
        else:
            q_output = output

    torch.cuda.empty_cache()
    model.train()
    # print(q_output.shape)
    print(q_output.shape, np.asarray(q_output).mean(),np.asarray(q_output).reshape(-1,256).min(),np.asarray(q_output).reshape(-1,256).max())
    return q_output


def get_recall(m, n, DATABASE_VECTORS, QUERY_VECTORS, QUERY_SETS):

    database_output = DATABASE_VECTORS[m]
    queries_output = QUERY_VECTORS[n]

    # print(len(queries_output))
    database_nbrs = KDTree(database_output)

    recall = [0] * recall_num
    top1_similarity_score = []
    one_percent_retrieved = 0
    # threshold之内对应百分之一
    threshold = max(int(round(len(database_output)/100.0)), 1)

    num_evaluated = 0
    # 遍历需要被评估的点云
    for i in range(len(queries_output)):
        # 得到该点云在第m个子图的真实检索点云序列
        true_neighbors = QUERY_SETS[n][i][m]
        if(len(true_neighbors) == 0):
            continue
        # 被评估数
        num_evaluated += 1
        # 得到该点云在第m个子图的实际检索点云序列
        distances, indices = database_nbrs.query(
            np.array([queries_output[i]]),k=recall_num)
        # 遍历recal_num得到在不同指标下的结果
        for j in range(len(indices[0])):
            # 如果第j个候选是真实值
            if indices[0][j] in true_neighbors:
                # 如果是top1 recall
                if(j == 0):
                    similarity = np.dot(queries_output[i], database_output[indices[0][j]])
                    top1_similarity_score.append(similarity)
                # 如果第j个候选是真实值，+1
                recall[j] += 1
                break
        # 如果前百分之一包含真实值，+1
        if len(list(set(indices[0][0:threshold]).intersection(set(true_neighbors)))) > 0:
            one_percent_retrieved += 1

    one_percent_recall = (one_percent_retrieved/float(num_evaluated))*100
    # 这里用cumsum因为第j个元素只代表第j个，不代表前j个
    recall = (np.cumsum(recall)/float(num_evaluated))*100
    return recall, top1_similarity_score, one_percent_recall


if __name__ == "__main__":
    if torch.cuda.device_count() > 1:
        para.model = nn.DataParallel(para.model)
        # net = torch.nn.parallel.DistributedDataParallel(net)
        log_string("Let's use "+ str(torch.cuda.device_count())+ " GPUs!")
    #
    # print_gpu("0")
    if not os.path.exists(para.args.pretrained_path):
        log_string("can't find pretrained model")
    else:
        if para.args.pretrained_path[-1] == "7":
            log_string("load pretrained model")
            para.model.load_state_dict(torch.load(para.args.pretrained_path), strict=False)
        else:
            log_string("load checkpoint")
            checkpoint = torch.load(para.args.pretrained_path)
            saved_state_dict = checkpoint['state_dict']
            starting_epoch = checkpoint['epoch'] + 1
            TOTAL_ITERATIONS = checkpoint['iter']
            para.model.load_state_dict(saved_state_dict, strict=False)

    print(evaluate_model(para.model))


def get_latent_vectors2(model, dict_to_process):
    model.eval()
    torch.cuda.empty_cache()
    is_training = False
    train_file_idxs = np.arange(0, len(dict_to_process.keys()))

    batch_num = cfg.EVAL_BATCH_SIZE * \
        (1 + cfg.EVAL_POSITIVES_PER_QUERY + cfg.EVAL_NEGATIVES_PER_QUERY)
    q_output = []
    for q_index in range(len(train_file_idxs)//batch_num):
        file_indices = train_file_idxs[q_index *
                                       batch_num:(q_index+1)*(batch_num)]
        file_names = []
        for index in file_indices:
            file_names.append(dict_to_process[index]["query"])
        queries = load_pc_files(file_names)

        with torch.no_grad():
            feed_tensor = torch.from_numpy(queries).float()
            feed_tensor = feed_tensor.unsqueeze(1)
            feed_tensor = feed_tensor.to(device)
            out = model(feed_tensor)

        out = out.detach().cpu().numpy()
        out = np.squeeze(out)

        #out = np.vstack((o1, o2, o3, o4))
        q_output.append(out)

    q_output = np.array(q_output)
    if(len(q_output) != 0):
        q_output = q_output.reshape(-1, q_output.shape[-1])

    # handle edge case
    index_edge = len(train_file_idxs) // batch_num * batch_num
    if index_edge < len(dict_to_process.keys()):
        file_indices = train_file_idxs[index_edge:len(dict_to_process.keys())]
        file_names = []
        for index in file_indices:
            file_names.append(dict_to_process[index]["query"])
        queries = load_pc_files(file_names)

        with torch.no_grad():
            feed_tensor = torch.from_numpy(queries).float()
            feed_tensor = feed_tensor.unsqueeze(1)
            feed_tensor = feed_tensor.to(device)
            o1 = model(feed_tensor)

        output = o1.detach().cpu().numpy()
        output = np.squeeze(output)
        if (q_output.shape[0] != 0):
            q_output = np.vstack((q_output, output))
        else:
            q_output = output
    torch.cuda.empty_cache()
    model.train()
    # print(q_output.shape)
    # print(q_output.shape, np.asarray(q_output).mean(),np.asarray(q_output).reshape(-1,256).min(),np.asarray(q_output).reshape(-1,256).max())
    return q_output

def evaluate_model2(model):

    if not os.path.exists(cfg.RESULTS_FOLDER):
        os.mkdir(cfg.RESULTS_FOLDER)

    recall = np.zeros(25)
    count = 0
    similarity = []
    one_percent_recall = []

    DATABASE_VECTORS = []
    QUERY_VECTORS = []

    for i in (range(len(DATABASE_SETS))):
        DATABASE_VECTORS.append(get_latent_vectors2(model, DATABASE_SETS[i]))

    for j in (range(len(QUERY_SETS))):
        QUERY_VECTORS.append(get_latent_vectors2(model, QUERY_SETS[j]))

    for m in (range(len(QUERY_SETS))):
        for n in range(len(QUERY_SETS)):
            if (m == n):
                continue
            pair_recall, pair_similarity, pair_opr = get_recall(
                m, n, DATABASE_VECTORS, QUERY_VECTORS, QUERY_SETS)
            recall += np.array(pair_recall)
            count += 1
            one_percent_recall.append(pair_opr)
            for x in pair_similarity:
                similarity.append(x)

    ave_recall = recall / count
    # print(ave_recall)

    # print(similarity)
    average_similarity = np.mean(similarity)
    # print(average_similarity)

    ave_one_percent_recall = np.mean(one_percent_recall)
    # print(ave_one_percent_recall)

    with open(cfg.OUTPUT_FILE, "w") as output:
        output.write("Average Recall @N:\n")
        output.write(str(ave_recall))
        output.write("\n\n")
        output.write("Average Similarity:\n")
        output.write(str(average_similarity))
        output.write("\n\n")
        output.write("Average Top 1% Recall:\n")
        output.write(str(ave_one_percent_recall))

    return ave_one_percent_recall