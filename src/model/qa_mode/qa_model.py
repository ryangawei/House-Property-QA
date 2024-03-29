# coding=utf-8

import sys
import os
import os.path as oph
import torch
import logging
from dataset.qa_data.dataset import QaDataset
from dataset.qa_data2.dataset import QaDataset2
from dataset.qa_data3.dataset import QaDataset3
from dataset.crmc.dataset import CRMCDataset
from model.metrics.eval_callback import EvaluateMetrics, Metrics, SaveModelCallback
import numpy as np
from tqdm import tqdm
from tensorboardX import SummaryWriter
import torch.nn as nn
from .model.bert_cls_model import BertCLSModel
from .model.bert_multi_hid_model import BertMultiHidModel
import torch.optim as optim
import time
from sklearn.metrics import f1_score


class QaModel():
    def __init__(self, bert_model_dir,
                 save_model_path,
                 eval_mode ,
                 args):

        ''' 创建log文件夹 '''
        self.log_file = oph.join(save_model_path, 'log%s.txt' % args.my_name)

        ''' 创建summary对象 '''
        summary_dir = oph.join(save_model_path, 'summary%s' % args.my_name)
        self.summary_writer = SummaryWriter(summary_dir)

        ''' 创建checkpoint文件夹 '''
        checkpoint = os.path.join(save_model_path, 'checkpoint')
        if not os.path.exists(checkpoint):
            os.makedirs(checkpoint)
        self.model_save_path = checkpoint

        ''' 创建logger对象 '''
        FORMAT = '%(filename)s->%(funcName)s(%(levelname)s): %(message)s'
        format = logging.Formatter(FORMAT)
        logger = logging.getLogger('Bert4Ir')
        logger.setLevel(logging.DEBUG)
        fhl = logging.FileHandler(filename=self.log_file, encoding='utf-8')
        fhl.setLevel(logging.DEBUG)
        fhl.setFormatter(format)
        logger.addHandler(fhl)
        self.log = logger

        localtime = time.asctime(time.localtime(time.time()))

        self.log.info('\n')
        self.log.info('Exp: %s ---------------------------------%s----------------------------------------' % (args.exp_name, localtime))
        ''' 保存超参 '''
        self.args = args
        logger.info(args)

        ''' 查看GPU数量 '''
        logger.info('GPU num: %d' % (torch.cuda.device_count()))

        ''' 根据模型和数据类型加载数据集 '''
        if 'qa_data3' in args.data_type:
            self.dataset = QaDataset3(vocab_dir=bert_model_dir, args=args)
        elif args.data_type == 'crmc':
            self.dataset = CRMCDataset(vocab_dir=bert_model_dir, args=args)
        else:
            RuntimeError('None model found !!')
        self.train_generator = self.dataset.train_generator
        self.test_generator = self.dataset.test_generator
        if args.have_val == True:
            self.dev_generator = self.dataset.dev_generator

        ''' 构建模型对象 '''
        if args.model_name == 'bert_cls_model':
            self.qa_model = BertCLSModel(bert_model_dir=bert_model_dir, args=args)
        if args.model_name == 'bert_multi_hid_model':
            self.qa_model = BertMultiHidModel(bert_model_dir=bert_model_dir, args=args)
        else:
            RuntimeError('None model found !!')

        ''' 得到loss函数 '''
        self.loss_fun = self.qa_model.get_loss_function()

        ''' 决定是否并行运行 '''
        if args.parallel:
            self.qa_model = nn.DataParallel(self.qa_model)



        ''' 冻结最后一层encoder之前的层 '''
        if args.freeze:
            self.log.info('Freeze layer %d' % args.freeze_layer)
            self.para_freezer(args.freeze_layer)
        else:
            self.log.info('No layer was freezed.' )


        ''' 构建优化器 '''
        params = filter(lambda p:p.requires_grad, self.qa_model.parameters())
        self.optimizer = optim.Adam(params, lr=args.lr)


        ''' 构建保存模型的回调 '''
        self.save_callback = SaveModelCallback(model=self.qa_model,
                                               optimizer=self.optimizer,
                                               save_model_path=checkpoint,
                                               args=args,
                                               log=self.log, always_save=args.always_save)

        ''' 构建三个数据集的评估回调函数 '''
        if self.args.have_val == True:
            self.eval_fun = EvaluateMetrics(metric=Metrics,
                                            args=args,
                                            generator=self.dev_generator,
                                            save_model_callback=None,
                                            mode=eval_mode,
                                            log=self.log)
            self.test_fun = EvaluateMetrics(metric=Metrics,
                                            args=args,
                                            generator=self.test_generator,
                                            save_model_callback=self.save_callback,
                                            mode=eval_mode,
                                            log=self.log)
        else:
            self.test_fun = EvaluateMetrics(metric=Metrics,
                                            args=args,
                                            generator=self.test_generator,
                                            save_model_callback=self.save_callback,
                                            mode=eval_mode,
                                            log=self.log)



        if args.eval_train:
            self.train_eval_fun = EvaluateMetrics(metric=Metrics,
                                                  args=args,
                                                  generator=self.train_generator,
                                                  save_model_callback=None,
                                                  mode=eval_mode,
                                                  log=self.log)


        ''' 加载已经存在的模型 '''
        self.epoch = 0
        if os.path.exists(os.path.join(checkpoint, '0', 'ckpt.pth')):
            self.log.debug('ckpt.pth exist! Loading...')
            max_epoch = 0
            model_path = ''
            '''加载最新的，即epoch标号最大的模型'''
            for root, dirs, files in os.walk(checkpoint):
                for d in dirs:
                    epoch_num = int(d)
                    if epoch_num >= max_epoch:
                        model_path = os.path.join(root, d, 'ckpt.pth')
                        if os.path.exists(model_path):
                            max_epoch = epoch_num
                            model_path = os.path.join(root, d, 'ckpt.pth')
            self.log.debug('Load %s ' % model_path)
            checkpoint = torch.load(model_path)
            self.qa_model.load_state_dict(checkpoint['model_state_dict'])

            ''' 如果加载预训练模型参数，则不需要加载logs epoch optimizer数据 '''
            if not self.args.load_pretrain_model:
                self.save_callback.now_best_val_log=checkpoint['val_logs']
                self.epoch = checkpoint['epoch'] + 1
                self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
                for state in self.optimizer.state.values():
                    for k, v in state.items():
                        if isinstance(v, torch.Tensor):
                            state[k] = v.to('cuda')


        self.qa_model.to('cuda')
        self.loss_fun.to('cuda')

    ''' k-fold 训练'''
    def train_k_fold(self, num_epoch=20, fold_num=5):
        ''' 保存初始参数 '''
        init_path = os.path.join(self.model_save_path, self.args.my_name, 'init')
        if not os.path.exists(init_path):
            os.makedirs(init_path)
            self.log.info("Saving init param to %s" % os.path.join(init_path, 'ckpt.pth'))
            torch.save(self.qa_model.state_dict(), os.path.join(init_path, 'ckpt.pth'))

        ''' k fold 训练 '''
        for fold_no in range(fold_num):
            ckpt = torch.load(os.path.join(init_path, 'ckpt.pth'))
            self.qa_model.load_state_dict(ckpt) # 重新加载参数
            self.train_generator.set_fold(fold_no) # 得到新的 fold
            self.test_generator.set_fold(fold_no) # 得到新的 fold
            self.test_fun.save_model_callback.reset_val_log() # 重置指标
            self.log.info('\n\nFold: %d Training:----------------------------------------------------------' % fold_no)
            for epoch in range(num_epoch):
                self.log.info('\nEpoch: %d Training:---------------------------------------------------------' % epoch)
                self.qa_model.zero_grad()
                self.optimizer.zero_grad()
                batches_bar = tqdm(range(len(self.train_generator)), desc='Training')
                predictions = []
                losses = []
                for index in batches_bar:
                    ''' 得到批次数据 '''
                    batch_data, rep_num = self.train_generator[index]
                    ''' 将数据导入 GPU'''
                    batch_data_cuda = []
                    for data in batch_data:
                        batch_data_cuda.append(data.to('cuda'))
                    batch_data_cuda = tuple(batch_data_cuda)

                    ''' 进行一次反向传播 '''
                    pre = self.qa_model(batch_data_cuda)
                    bz = list(pre[0].size())[0]
                    loss = self.loss_fun(pre[0][:int(rep_num*bz)], batch_data_cuda[-1][:int(rep_num*bz)])
                    loss = loss / self.args.accumulation_loss_step
                    loss.backward()
                    if (index + 1) % self.args.accumulation_loss_step ==0:
                        self.optimizer.step()
                        self.optimizer.zero_grad()

                    ''' 记录loss '''
                    tmp_loss = loss * self.args.accumulation_loss_step
                    losses.append(tmp_loss.item())

                    ''' 记录每个batch的结果 '''
                    bz = pre[1].size(0)
                    out = pre[1][:int(bz*rep_num)]
                    out = out.cpu().detach().numpy()
                    predictions.append(out)

                '''得到该次epoch平均loss'''
                avg_loss = sum(losses) / len(losses)
                self.log.info('Average loss : %f ' % avg_loss)

                ''' 对训练数据进行一次评估 '''
                pred = np.concatenate(predictions, axis=0)
                if pred.shape[-1] >= 2:
                    pred = np.argmax(pred, axis=-1)
                self.train_eval_fun.eval(epoch=epoch, pred=pred, step=epoch,summary_writer=self.summary_writer, mode='Train')

                ''' 对测试数据进行评估 '''
                self.log.info('Epoch: %d Testing-------------------------------------------------------------------------' % epoch)
                path = os.path.join(self.model_save_path, self.args.my_name, 'fold_%d' % fold_no)
                self.test_fun.save_model_callback.set_save_path(path)
                self.test(epoch)
                # time.sleep(10)

                ''' 融合兄弟模型参数 '''
                if self.args.bro_name != '':
                    my_ckpt_path = os.path.join(self.model_save_path, self.args.my_name,
                                                   'fold_%d/%d'%(fold_no, epoch), 'ckpt.pth')
                    self.log.info('Loading my ckpt %s ...' % my_ckpt_path)
                    my_ckpt = torch.load(my_ckpt_path)

                    bro_ckpt_path = os.path.join(self.model_save_path, self.args.bro_name,
                                            'fold_%d/%d'%(fold_no, epoch), 'ckpt.pth')
                    while not os.path.exists(bro_ckpt_path):
                        self.log.info('Waiting for brother ckpt.pth for 5s ...')
                        time.sleep(5)
                    self.log.info('Loading bro ckpt %s after 10s ...' % bro_ckpt_path)
                    time.sleep(10)

                    bro_ckpt = torch.load(bro_ckpt_path)
                    bro_state_dict = bro_ckpt['model_state_dict']
                    my_state_dict = my_ckpt['model_state_dict']

                    model_state_dict = {}
                    for my, bro in zip(my_state_dict.items(), bro_state_dict.items()):
                        assert my[0] == bro[0]
                        my_para = my[1]
                        bro_para = bro[1]
                        model_state_dict[my[0]] = 0.5*my_para + 0.5*bro_para
                    self.qa_model.load_state_dict(model_state_dict)

    ''' 得到最好的epoch所在的路径 '''
    def get_best_result_epoch(self, path):
        max_epoch = 0
        model_path = ''
        for root, dirs, files in os.walk(path):
            for d in dirs:
                epoch_num = int(d)
                if epoch_num >= max_epoch:
                    model_path = os.path.join(root, d, 'ckpt.pth')
                    if os.path.exists(model_path):
                        max_epoch = epoch_num
                        model_path = os.path.join(root, d, 'ckpt.pth')
        self.log.debug('Load %s ' % model_path)
        return model_path

    ''' 得到整个数据集指标，并保存评估数据集结果 '''
    def get_best_result(self, epochs=None, fold_num=5):
        test_results = np.zeros((self.test_generator.batcher.total_sample_num, )) # [total_num, ] # 测试结果
        test_labels = np.array(self.test_generator.batcher.total_sample_label)  # [total_num, ] # 测试标签
        final_result_list = [] # 上传结果
        for fold in range(fold_num):
            final_result = [] # 评估数据指标
            test_part_res = [] #  部分测试数据指标

            '''加载最好的，即epoch标号最大的模型'''
            self.log.info('Loading best ckpt in fold: %d ------------------------- ' % fold)
            path = os.path.join(self.model_save_path, self.args.my_name , 'fold_%d'%fold)
            if epochs:
                model_path = os.path.join(path, epochs[fold], 'ckpt.pth')
            else:
                model_path = self.get_best_result_epoch(path)
            self.log.info('Loading ckpt %s' % model_path)
            checkpoint = torch.load(model_path)
            self.qa_model.load_state_dict(checkpoint['model_state_dict'])

            ''' 得到一fold 测试数据 '''
            self.test_generator.set_fold(fold)
            test_index = self.test_generator.batcher.test_set_list[fold].tolist()

            self.qa_model.eval()
            with torch.no_grad():
                ''' 测试一fold数据 '''
                for batch_data, rep_num in tqdm(self.test_generator):
                    ''' 将数据导入 GPU'''
                    batch_data_cuda = []
                    for data in batch_data:
                        batch_data_cuda.append(data.to('cuda'))
                    batch_data_cuda = tuple(batch_data_cuda)

                    ''' 预测结果 '''
                    out = self.qa_model(batch_data_cuda)[1]
                    bz = out.size(0)
                    out = out[:int(bz * rep_num)]
                    out = out.cpu().detach().numpy()
                    test_part_res.append(out)
                test_part_res = np.concatenate(test_part_res, axis=0)
                ''' 评估本次结果 '''
                self.test_fun.save_model_callback = None
                self.test_fun.eval(epoch=0, pred=test_part_res, step=0, summary_writer=None, mode='test')

                test_results[test_index] = np.squeeze(test_part_res)


                ''' 测试评估数据集 '''
                for batch_data, rep_num in tqdm(self.dev_generator):
                    ''' 将数据导入 GPU'''
                    batch_data_cuda = []
                    for data in batch_data:
                        batch_data_cuda.append(data.to('cuda'))
                    batch_data_cuda = tuple(batch_data_cuda)

                    ''' 预测结果 '''
                    out = self.qa_model(batch_data_cuda)[1]
                    bz = out.size(0)
                    out = out[:int(bz * rep_num)]
                    out = out.cpu().detach().numpy()
                    final_result.append(out)
                final_result = np.concatenate(final_result, axis=0)
                final_result_list.append(final_result)

        ''' 得到整个数据集上的指标 '''
        assert not any(test_results == 0)
        total_f1, best_thre = self.search_f1(test_labels, test_results)
        logging.info('Total f1 %f, best thre %f' % (total_f1, best_thre))

        ''' 得到评估数据集的结果，并保存到本地 '''
        final_result_list = np.concatenate(final_result_list, axis=-1)
        final_result_list = np.mean(final_result_list, axis=-1)
        final_result_list = final_result_list > best_thre
        submission_file = os.path.join(self.model_save_path, self.args.my_name,'submission_beike_{}.csv'.format(total_f1))
        self.dev_generator.batcher.df_test['label'] = final_result_list.astype(int)
        self.dev_generator.batcher.df_test[['id', 'id_sub', 'label']].to_csv(submission_file, index=False, header=None, sep='\t')

    def search_f1(self, y_true, y_pred):
        best = 0
        best_t = 0.0
        for i in range(30, 60):
            tres = i / 100
            y_pred_bin = (y_pred > tres).astype(int)
            score = f1_score(y_true, y_pred_bin)
            if score > best:
                best = score
                best_t = tres

        # y_pred[y_pred >= best_t] = 1.0
        # y_pred[y_pred < best_t] = 0.0
        # best = f1_score(y_true, y_pred)

        self.log.info('best %f' % best)
        self.log.info('thres %f' % best_t)
        return best, best_t

    ''' 训练循环 '''
    def train_loop(self, num_epoch=100):
        epoch = self.epoch
        self.qa_model.zero_grad()
        self.optimizer.zero_grad()
        step = epoch*self.args.batch_num_per_epoch

        for _ in range(num_epoch - epoch):
            self.qa_model.train()
            self.log.info('\nEpoch: %d Training:----------------------------------------------------------------------' % epoch)
            batches_bar = tqdm(range(len(self.train_generator)), desc='Training')
            # batches_bar = tqdm(range(4), desc='Training')
            predictions = []
            losses = []
            labels = []
            for index in batches_bar:
                ''' 得到批次数据 '''
                batch_data, rep_num = self.train_generator[index]

                batch_data_cuda = []
                ''' 将数据导入 GPU'''
                for data in batch_data:
                    batch_data_cuda.append(data.to('cuda'))
                batch_data_cuda = tuple(batch_data_cuda)

                ''' 进行一次反向传播 '''
                pre = self.qa_model(batch_data_cuda)
                bz = list(pre[0].size())[0]
                loss = self.loss_fun(pre[0][:int(rep_num*bz)], batch_data_cuda[-1][:int(rep_num*bz)])
                loss = loss / self.args.accumulation_loss_step
                loss.backward()
                if (index + 1) % self.args.accumulation_loss_step ==0:
                    self.optimizer.step()
                    self.optimizer.zero_grad()

                ''' 记录loss '''
                tmp_loss = loss*self.args.accumulation_loss_step
                losses.append(tmp_loss.item())
                batches_bar.set_description("Training Loss: %f" % tmp_loss.item())
                self.summary_writer.add_scalar('Train/loss', tmp_loss.item(), step)
                self.summary_writer.flush()
                step += 1

                ''' 记录每个batch的结果 '''
                bz = list(pre[1].size())[0]
                out = pre[1][:int(bz*rep_num)]
                out = out.cpu().detach().numpy()

                predictions.append(out)

                ''' 记录每个batch的label '''
                bz = list(pre[1].size())[0]
                l = batch_data_cuda[-1][:int(bz*rep_num)]
                l = l.cpu().detach().numpy()
                labels.append(l)


            '''得到该次epoch平均loss'''
            avg_loss = sum(losses)/len(losses)
            self.log.info('Average loss : %f ' % avg_loss)
            self.summary_writer.add_scalar('Train/avg_loss', avg_loss, epoch)
            self.summary_writer.flush()


            ''' 对训练数据进行一次评估 '''
            if self.args.eval_train:
                pred = np.concatenate(predictions, axis=0)
                if pred.shape[-1] >= 2:
                    pred = np.argmax(pred, axis=-1)

                self.train_eval_fun.set_thre(0.5)
                self.train_eval_fun.eval(epoch=epoch, pred=pred, step=epoch, summary_writer=self.summary_writer, mode='Train')


            if self.args.eval:
                if self.args.have_val == True:
                    ''' 对验证数据进行一次评估 '''
                    self.log.info('Epoch: %d Evaluating----------------------------------------------------------------------' % epoch)
                    self.evaluate(epoch)

                ''' 对测试数据进行评估 '''
                self.log.info('Epoch: %d Testing-------------------------------------------------------------------------' % epoch)
                self.test(epoch)

            epoch += 1

    def evaluate(self, epoch):
        self.qa_model.eval()
        with torch.no_grad():
            res = []
            for i in tqdm(range(len(self.dev_generator)), desc='Evaluating'):
                ''' 得到数据 '''
                try:
                    batch_data, rep_num = self.dev_generator[i]
                except StopIteration:
                    break

                batch_data_cuda = []
                ''' 将数据导入 GPU'''
                for data in batch_data:
                    batch_data_cuda.append(data.cuda())
                batch_data_cuda = tuple(batch_data_cuda)
                ''' 预测结果 '''
                out = self.qa_model(batch_data_cuda)[1]
                bz = list(out.size())[0]
                out = out[:int(rep_num*bz)]
                out = out.cpu().detach().numpy()
                res.append(out)
            ''' 进行评估 '''
            pred = np.concatenate(res, axis=0)
            if pred.shape[-1] >= 2:
                pred = np.argmax(pred, axis=-1)

            self.eval_fun.set_thre(0.5)
            self.eval_fun.eval(epoch=epoch, pred=pred, step=epoch, summary_writer=self.summary_writer, mode='Eval')

    def test(self, epoch):
        self.qa_model.eval()
        with torch.no_grad():
            res = []
            for i in tqdm(range(len(self.test_generator)), desc='Testing'):
                ''' 得到数据 '''
                try:
                    batch_data, rep_num = self.test_generator[i]
                except StopIteration:
                    break

                batch_data_cuda = []
                ''' 将数据导入 GPU'''
                for data in batch_data:
                    batch_data_cuda.append(data.cuda())
                batch_data_cuda = tuple(batch_data_cuda)
                ''' 预测结果 '''
                out = self.qa_model(batch_data_cuda)[1]
                bz = list(out.size())[0]
                out = out[:int(bz*rep_num)]
                out = out.cpu().detach().numpy()
                res.append(out)
            ''' 进行评估 '''
            pred = np.concatenate(res, axis=0)
            if pred.shape[-1] >= 2:
                pred = np.argmax(pred, axis=-1)

            self.test_fun.set_thre(0.5)
            self.test_fun.eval(epoch=epoch, pred=pred, step=epoch, summary_writer=self.summary_writer, mode='Test')

    def para_freezer(self, freeze_layer):
        for i, p in enumerate(self.qa_model.parameters()):
            if i <= freeze_layer:  # 196:
                p.requires_grad = False

    def show_weight(self):
        print('----------------------------------------------------')
        dict_name = self.qa_model.state_dict().keys()
        for i,p in enumerate(dict_name):
            print(i,' ', p)
        print('----------------------------------------------------')









