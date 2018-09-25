import argparse
import collections
import os
import pickle
import pandas as pd
import pydicom
import skimage.transform

import numpy as np
import torch
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader
from torchvision import datasets, models, transforms
from tqdm import tqdm
import metric

import pytorch_retinanet.model
import pytorch_retinanet.model_se_resnext
import pytorch_retinanet.model_dpn
import pytorch_retinanet.model_pnasnet
import pytorch_retinanet.dataloader

import config
import utils
from config import CROP_SIZE, TEST_DIR
import matplotlib.pyplot as plt

import detection_dataset
from detection_dataset import DetectionDataset
from logger import Logger


class ModelInfo:
    def __init__(self,
                 factory,
                 args,
                 batch_size,
                 dataset_args,
                 img_size=512):
        self.factory = factory
        self.args = args
        self.batch_size = batch_size
        self.dataset_args = dataset_args
        self.img_size = img_size


MODELS = {
    'resnet34_512': ModelInfo(
        factory=pytorch_retinanet.model.resnet34,
        args=dict(num_classes=1, pretrained=True),
        img_size=512,
        batch_size=8,
        dataset_args=dict()
    ),
    'resnet101_512': ModelInfo(
        factory=pytorch_retinanet.model.resnet101,
        args=dict(num_classes=1, pretrained=True),
        img_size=512,
        batch_size=4,
        dataset_args=dict()
    ),
    'resnet152_512': ModelInfo(
        factory=pytorch_retinanet.model.resnet152,
        args=dict(num_classes=1, pretrained=True),
        img_size=512,
        batch_size=4,
        dataset_args=dict()
    ),
    'se_resnext101_512': ModelInfo(
        factory=pytorch_retinanet.model_se_resnext.se_resnext101,
        args=dict(num_classes=1, pretrained=True),
        img_size=512,
        batch_size=3,
        dataset_args=dict()
    ),
    'se_resnext101_512_bs12': ModelInfo(
        factory=pytorch_retinanet.model_se_resnext.se_resnext101,
        args=dict(num_classes=1, pretrained=True),
        img_size=512,
        batch_size=12,
        dataset_args=dict()
    ),
    'se_resnext101_256': ModelInfo(
        factory=pytorch_retinanet.model_se_resnext.se_resnext101,
        args=dict(num_classes=1, pretrained=True),
        img_size=256,
        batch_size=12,
        dataset_args=dict()
    ),
    'resnet34_256': ModelInfo(
        factory=pytorch_retinanet.model.resnet34,
        args=dict(num_classes=1, pretrained=True),
        img_size=256,
        batch_size=32,
        dataset_args=dict()
    ),
    'dpn92_256': ModelInfo(
        factory=pytorch_retinanet.model_dpn.dpn92,
        args=dict(num_classes=1, pretrained=True),
        img_size=256,
        batch_size=4,
        dataset_args=dict()
    ),
    'dpn92_512': ModelInfo(
        factory=pytorch_retinanet.model_dpn.dpn92,
        args=dict(num_classes=1, pretrained=True),
        img_size=512,
        batch_size=4,
        dataset_args=dict()
    ),
    'pnas_256': ModelInfo(
        factory=pytorch_retinanet.model_pnasnet.pnasnet5large,
        args=dict(num_classes=1, pretrained=True),
        img_size=256,
        batch_size=8,
        dataset_args=dict()
    ),
    'pnas_512': ModelInfo(
        factory=pytorch_retinanet.model_pnasnet.pnasnet5large,
        args=dict(num_classes=1, pretrained=True),
        img_size=512,
        batch_size=4,
        dataset_args=dict()
    ),
    'pnas_512_bs12': ModelInfo(
        factory=pytorch_retinanet.model_pnasnet.pnasnet5large,
        args=dict(num_classes=1, pretrained=True),
        img_size=512,
        batch_size=8,
        dataset_args=dict()
    ),
}


def train(model_name, fold, run=None):
    model_info = MODELS[model_name]

    run_str = '' if run is None or run == '' else f'_{run}'

    checkpoints_dir = f'checkpoints/{model_name}{run_str}_fold_{fold}'
    tensorboard_dir = f'../output/tensorboard/{model_name}{run_str}_fold_{fold}'
    predictions_dir = f'../output/oof/{model_name}{run_str}_fold_{fold}'
    os.makedirs(checkpoints_dir, exist_ok=True)
    os.makedirs(tensorboard_dir, exist_ok=True)
    os.makedirs(predictions_dir, exist_ok=True)
    print('\n', model_name, '\n')

    logger = Logger(tensorboard_dir)

    retinanet = model_info.factory(**model_info.args)
    retinanet = retinanet.cuda()
    retinanet = torch.nn.DataParallel(retinanet).cuda()

    dataset_train = DetectionDataset(fold=fold, img_size=model_info.img_size, is_training=True, images={})
    dataset_valid = DetectionDataset(fold=fold, img_size=model_info.img_size, is_training=False, images={})

    dataloader_train = DataLoader(dataset_train,
                                  num_workers=16,
                                  batch_size=model_info.batch_size,
                                  shuffle=True,
                                  drop_last=True,
                                  collate_fn=pytorch_retinanet.dataloader.collater2d)

    dataloader_valid = DataLoader(dataset_valid,
                                  num_workers=8,
                                  batch_size=4,
                                  shuffle=False,
                                  drop_last=True,
                                  collate_fn=pytorch_retinanet.dataloader.collater2d)

    retinanet.training = True

    optimizer = optim.Adam(retinanet.parameters(), lr=1e-5)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=4, verbose=True, factor=0.2)

    retinanet.train()
    retinanet.module.freeze_bn()

    print('Num training images: {}'.format(len(dataset_train)))
    epochs = 512

    for epoch_num in range(epochs):

        retinanet.train()
        retinanet.module.freeze_bn()
        if epoch_num < 1:
            retinanet.module.freeze_encoder()

        epoch_loss = []
        loss_cls_hist = []
        loss_cls_global_hist = []
        loss_reg_hist = []

        data_iter = tqdm(enumerate(dataloader_train), total=len(dataloader_train))
        for iter_num, data in data_iter:
            optimizer.zero_grad()
            inputs = [data['img'].cuda().float(), data['annot'].cuda().float(), data['category'].cuda()]
            # print([i.shape for i in inputs])

            classification_loss, regression_loss, global_classification_loss = \
                retinanet(inputs, return_loss=True, return_boxes=False)

            classification_loss = classification_loss.mean()
            regression_loss = regression_loss.mean()
            global_classification_loss = global_classification_loss.mean()

            loss = classification_loss + regression_loss + global_classification_loss

            # if bool(loss == 0):
            #     continue

            loss.backward()

            torch.nn.utils.clip_grad_norm_(retinanet.parameters(), 0.05)

            optimizer.step()

            loss_cls_hist.append(float(classification_loss))
            loss_cls_global_hist.append(float(global_classification_loss))
            loss_reg_hist.append(float(regression_loss))
            epoch_loss.append(float(loss))

            data_iter.set_description(
                f'{epoch_num} cls: {np.mean(loss_cls_hist):1.4f} cls g: {np.mean(loss_cls_global_hist):1.4f} Reg: {np.mean(loss_reg_hist):1.4f} Loss: {np.mean(epoch_loss):1.4f}')

            del classification_loss
            del regression_loss

        logger.scalar_summary('loss_train', np.mean(epoch_loss), epoch_num)
        logger.scalar_summary('loss_train_classification', np.mean(loss_cls_hist), epoch_num)
        logger.scalar_summary('loss_train_global_classification', np.mean(loss_cls_global_hist), epoch_num)
        logger.scalar_summary('loss_train_regression', np.mean(loss_reg_hist), epoch_num)

        # validation
        with torch.no_grad():
            retinanet.eval()

            loss_hist_valid = []
            loss_cls_hist_valid = []
            loss_cls_global_hist_valid = []
            loss_reg_hist_valid = []

            # oof = collections.defaultdict(list)

            data_iter = tqdm(enumerate(dataloader_valid), total=len(dataloader_valid))
            for iter_num, data in data_iter:
                res = retinanet([data['img'].cuda().float(), data['annot'].cuda().float(), data['category'].cuda()],
                                       return_loss=True, return_boxes=False)
                # classification_loss, regression_loss, global_classification_loss, nms_scores, global_class, transformed_anchors = res
                classification_loss, regression_loss, global_classification_loss = res


                # oof['gt_boxes'].append(data['annot'].cpu().numpy().copy())
                # oof['gt_category'].append(data['category'].cpu().numpy().copy())
                # oof['boxes'].append(transformed_anchors.cpu().numpy().copy())
                # oof['scores'].append(nms_scores.cpu().numpy().copy())
                # oof['category'].append(global_class.cpu().numpy().copy())

                classification_loss = classification_loss.mean()
                regression_loss = regression_loss.mean()
                global_classification_loss = global_classification_loss.mean()
                loss = classification_loss + regression_loss + global_classification_loss

                loss_hist_valid.append(float(loss))
                loss_cls_hist_valid.append(float(classification_loss))
                loss_cls_global_hist_valid.append(float(global_classification_loss))
                loss_reg_hist_valid.append(float(regression_loss))

                data_iter.set_description(
                    f'{epoch_num} cls: {np.mean(loss_cls_hist_valid):1.4f} cls g: {np.mean(loss_cls_global_hist_valid):1.4f} Reg: {np.mean(loss_reg_hist_valid):1.4f} Loss {np.mean(loss_hist_valid):1.4f}')

                del classification_loss
                del regression_loss

            logger.scalar_summary('loss_valid', np.mean(loss_hist_valid), epoch_num)
            logger.scalar_summary('loss_valid_classification', np.mean(loss_cls_hist_valid), epoch_num)
            logger.scalar_summary('loss_valid_global_classification', np.mean(loss_cls_global_hist_valid), epoch_num)
            logger.scalar_summary('loss_valid_regression', np.mean(loss_reg_hist_valid), epoch_num)

            # pickle.dump(oof, open(f'{predictions_dir}/{epoch_num:03}.pkl', 'wb'))
            #
            # np.savez(f'{predictions_dir}/{epoch_num:03}.npz',
            #          gt_boxes=np.concatenate(oof['gt_boxes'], axis=0),
            #          gt_category=np.concatenate(oof['gt_category'], axis=0),
            #          boxes=np.concatenate(oof['boxes'], axis=0),
            #          scores=np.concatenate(oof['scores'], axis=0),
            #          category=np.concatenate(oof['category'], axis=0)
            #          )

        scheduler.step(np.mean(epoch_loss))
        # if epoch_num % 4 == 0:
        torch.save(retinanet.module, f'{checkpoints_dir}/{model_name}_{epoch_num:03}.pt')

    retinanet.eval()
    torch.save(retinanet, f'{checkpoints_dir}/{model_name}_final.pt')


def check(model_name, fold, checkpoint):
    model_info = MODELS[model_name]
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    model = torch.load(checkpoint, map_location=device)
    model = model.to(device)
    model.eval()

    dataset_valid = DetectionDataset(fold=fold, img_size=model_info.img_size, is_training=False,
                                     images={})

    dataloader_valid = DataLoader(dataset_valid,
                                  num_workers=1,
                                  batch_size=1,
                                  shuffle=False,
                                  collate_fn=pytorch_retinanet.dataloader.collater2d)

    data_iter = tqdm(enumerate(dataloader_valid), total=len(dataloader_valid))
    for iter_num, data in data_iter:
        classification_loss, regression_loss, global_classification_loss, nms_scores, nms_class, transformed_anchors = \
            model([data['img'].to(device).float(), data['annot'].to(device).float(), data['category'].cuda()],
                  return_loss=True, return_boxes=True)

        nms_scores = nms_scores.cpu().detach().numpy()
        nms_class = nms_class.cpu().detach().numpy()
        transformed_anchors = transformed_anchors.cpu().detach().numpy()

        print(nms_scores, transformed_anchors.shape)
        print('cls loss:', float(classification_loss), 'global cls loss:', global_classification_loss, ' reg loss:', float(regression_loss))
        print('cat:', data['category'].numpy()[0], np.exp(nms_class[0]), dataset_valid.categories[data['category'][0]])

        plt.cla()
        plt.imshow(data['img'][0, 0].cpu().detach().numpy())

        gt = data['annot'].cpu().detach().numpy()[0]
        for i in range(gt.shape[0]):
            if np.all(np.isfinite(gt[i])):
                p0 = gt[i, 0:2]
                p1 = gt[i, 2:4]
                plt.gca().add_patch(
                    plt.Rectangle(p0, width=(p1 - p0)[0], height=(p1 - p0)[1], fill=False, edgecolor='b', linewidth=2))

        for i in range(len(nms_scores)):
            nms_score = nms_scores[i]
            if nms_score < 0.1:
                break
            # print(transformed_anchors[i, :])

            p0 = transformed_anchors[i, 0:2]
            p1 = transformed_anchors[i, 2:4]

            color = 'g'
            if nms_score < 0.4:
                color = 'y'
            if nms_score < 0.25:
                color = 'r'

            # print(p0, p1)
            plt.gca().add_patch(plt.Rectangle(p0, width=(p1-p0)[0], height=(p1-p0)[1], fill=False, edgecolor=color, linewidth=2))
            plt.gca().text(p0[0], p0[1], f'{nms_score:.3f}', color=color)  # , bbox={'facecolor': color, 'alpha': 0.5})
        plt.show()

        print(nms_scores)


def generate_predictions(model_name, run, fold, from_epoch=4, to_epoch=100):
    run_str = '' if run is None or run == '' else f'_{run}'
    predictions_dir = f'../output/oof2/{model_name}{run_str}_fold_{fold}'
    os.makedirs(predictions_dir, exist_ok=True)

    model_info = MODELS[model_name]
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    for epoch_num in range(from_epoch, to_epoch):
        prediction_fn = f'{predictions_dir}/{epoch_num:03}.pkl'
        if os.path.exists(prediction_fn):
            continue
        print('epoch', epoch_num)
        checkpoint = f'checkpoints/{model_name}{run_str}_fold_{fold}/{model_name}_{epoch_num:03}.pt'
        try:
            model = torch.load(checkpoint, map_location=device)
        except FileNotFoundError:
            break
        model = model.to(device)
        model.eval()

        dataset_valid = DetectionDataset(fold=fold, img_size=model_info.img_size, is_training=False,
                                         images={})

        dataloader_valid = DataLoader(dataset_valid,
                                      num_workers=2,
                                      batch_size=1,
                                      shuffle=False,
                                      collate_fn=pytorch_retinanet.dataloader.collater2d)

        oof = collections.defaultdict(list)

        # for iter_num, data in tqdm(enumerate(dataloader_valid), total=len(dataloader_valid)):
        for iter_num, data in tqdm(enumerate(dataset_valid), total=len(dataloader_valid)):
            data = pytorch_retinanet.dataloader.collater2d([data])
            img = data['img'].to(device).float()
            nms_scores, global_classification, transformed_anchors = \
                model(img, return_loss=False, return_boxes=True)

            nms_scores = nms_scores.cpu().detach().numpy()
            global_classification = global_classification.cpu().detach().numpy()
            transformed_anchors = transformed_anchors.cpu().detach().numpy()

            oof['gt_boxes'].append(data['annot'].cpu().detach().numpy())
            oof['gt_category'].append(data['category'].cpu().detach().numpy())

            oof['boxes'].append(transformed_anchors)
            oof['scores'].append(nms_scores)
            oof['category'].append(global_classification)

        pickle.dump(oof, open(prediction_fn, 'wb'))


def p1p2_to_xywh(p1p2):
    xywh = np.zeros((p1p2.shape[0], 4))

    xywh[:, :2] = p1p2[:, :2]
    xywh[:, 2:4] = p1p2[:, 2:4] - p1p2[:, :2]
    return xywh


def check_metric(model_name, run, fold):
    run_str = '' if run is None or run == '' else f'_{run}'
    predictions_dir = f'../output/oof2/{model_name}{run_str}_fold_{fold}'
    thresholds = [0.05, 0.075, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4, 1.6, 2.0, 3.0, 4.0]

    all_scores = []

    for epoch_num in range(100):
        fn = f'{predictions_dir}/{epoch_num:03}.pkl'
        try:
            oof = pickle.load(open(fn, 'rb'))
        except FileNotFoundError:
            continue

        print('epoch ', epoch_num)
        epoch_scores = []
        nb_images = len(oof['scores'])
        for threshold in thresholds:
            threshold_scores = []
            for img_id in range(nb_images):
                gt_boxes = oof['gt_boxes'][img_id][0].copy()

                boxes = oof['boxes'][img_id].copy()
                scores = oof['scores'][img_id].copy()
                category = oof['category'][img_id]

                category = np.exp(category[0, 2])

                if len(scores):
                    scores[scores < scores[0]*0.5] = 0.0

                    # if category > 0.5 and scores[0] < 0.2:
                    #     scores[0] *= 2

                # mask = scores * category * 10 > threshold
                mask = scores * 5 > threshold

                if gt_boxes[0, 4] == -1.0:
                    if np.any(mask):
                        threshold_scores.append(0.0)
                else:
                    if len(scores[mask]) == 0:
                        score = 0.0
                    else:
                        score = metric.map_iou(
                            boxes_true=p1p2_to_xywh(gt_boxes),
                            boxes_pred=p1p2_to_xywh(boxes[mask]),
                            scores=scores[mask])
                    # print(score)
                    threshold_scores.append(score)

            print(threshold, np.mean(threshold_scores))
            epoch_scores.append(np.mean(threshold_scores))
        all_scores.append(epoch_scores)

    print('best score', np.max(all_scores))
    plt.imshow(np.array(all_scores))
    plt.show()


def prepare_submission(model_name, run, fold, epoch_num):
    run_str = '' if run is None or run == '' else f'_{run}'
    predictions_dir = f'../output/oof2/{model_name}{run_str}_fold_{fold}'
    os.makedirs(predictions_dir, exist_ok=True)

    model_info = MODELS[model_name]
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    checkpoint = f'checkpoints/{model_name}{run_str}_fold_{fold}/{model_name}_{epoch_num:03}.pt'
    model = torch.load(checkpoint, map_location=device)
    model = model.to(device)
    model.eval()

    sample_submission = pd.read_csv('../input/stage_1_sample_submission.csv')

    img_size = model_info.img_size
    submission = open('../submissions/sub5.csv', 'w')
    submission.write('patientId,PredictionString\n')

    for patient_id in sample_submission.patientId:
        dcm_data = pydicom.read_file(f'{config.TEST_DIR}/{patient_id}.dcm')
        img = dcm_data.pixel_array
        # img = img / 255.0
        img = skimage.transform.resize(img, (img_size, img_size), order=1)
        # utils.print_stats('img', img)

        img_tensor = torch.zeros(1, img_size, img_size, 1)
        img_tensor[0, :, :, 0] = torch.from_numpy(img)
        img_tensor = img_tensor.permute(0, 3, 1, 2)

        nms_scores, global_classification, transformed_anchors = \
            model(img_tensor.cuda(), return_loss=False, return_boxes=True)

        scores = nms_scores.cpu().detach().numpy()
        category = global_classification.cpu().detach().numpy()
        boxes = transformed_anchors.cpu().detach().numpy()
        category = np.exp(category[0, 2])

        if len(scores):
            scores[scores < scores[0] * 0.5] = 0.0

            # if category > 0.5 and scores[0] < 0.2:
            #     scores[0] *= 2

        # threshold = 0.25
        # mask = scores * category * 10 > threshold

        threshold = 0.5
        mask = scores * 5 > threshold

        submission_str = ''

        # plt.imshow(dcm_data.pixel_array)

        if np.any(mask):
            boxes_selected = p1p2_to_xywh(boxes[mask])  # x y w h format
            boxes_selected *= 1024.0 / img_size
            scores_selected = scores[mask]

            for i in range(scores_selected.shape[0]):
                x, y, w, h = boxes_selected[i]
                submission_str += f' {scores_selected[i]:.3f} {x:.1f} {y:.1f} {w:.1f} {h:.1f}'
                # plt.gca().add_patch(plt.Rectangle((x,y), width=w, height=h, fill=False, edgecolor='r', linewidth=2))

        print(f'{patient_id},{submission_str}      {category:.2f}')
        submission.write(f'{patient_id},{submission_str}\n')
        # plt.show()





if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', type=str, default='check')
    parser.add_argument('--model', type=str, default='')
    parser.add_argument('--run', type=str, default='')
    parser.add_argument('--fold', type=int, default=-1)
    parser.add_argument('--weights', type=str, default='')
    parser.add_argument('--epoch', type=int, default=-1)
    parser.add_argument('--from-epoch', type=int, default=2)
    parser.add_argument('--to-epoch', type=int, default=100)

    args = parser.parse_args()
    action = args.action
    model = args.model
    fold = args.fold

    if action == 'train':
        train(model_name=model, run=args.run, fold=args.fold)

    if action == 'check':
        if args.epoch > -1:
            run_str = '' if args.run is None or args.run == '' else f'_{args.run}'
            weights = f'checkpoints/{args.model_name}{run_str}_fold_{fold}/{args.model_name}_{args.epoch:03}.pt'
        else:
            weights = args.weighs

        check(model_name=model, fold=args.fold, checkpoint=weights)

    if action == 'check_metric':
        check_metric(model_name=model, run=args.run, fold=args.fold)

    if action == 'generate_predictions':
        generate_predictions(model_name=model, run=args.run, fold=args.fold,
                             from_epoch=args.from_epoch, to_epoch=args.to_epoch)

    if action == 'prepare_submission':
        prepare_submission(model_name=model, run=args.run, fold=args.fold, epoch_num=args.epoch)
