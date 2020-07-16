import math
import sys
import time
import shutil
import torch
from model_train import detection_util as utils
from models.resnet import ResNet
import torch
import torch.nn as nn
import torchvision
from torchvision import datasets,transforms
from transformers import AdamW,get_linear_schedule_with_warmup
from tqdm import trange,tqdm
import json
import pickle
from config import get_light_mask_rcnn_config
from model_train.coco_utils import get_coco_api_from_dataset,get_coco,ConvertCocoPolysToMask
from model_train.coco_eval import CocoEvaluator
from models.light_mask_rcnn_model import get_mask_rcnn_model,get_pruned_config
from utils import date2str

def save_checkpoint(state,save_path,filename='checkpoint.{0}.pth.tar',timestamp=''):
    filename = save_path + filename
    torch.save(state, filename.format(timestamp))

def collate_fn(batch):
    return tuple(zip(*batch))

def updateBn(model,decay_param):
    for m in model.parameters():
        if isinstance(m,nn.BatchNorm2d):
            m.weight.grad.data.add_(decay_param*torch.sign(m.weight.data))

def get_coco_dataloader(root_path,batch,Train=True):
    transform_list = []
    data_type ="val"
    if Train:
        data_type = "train"
    dataset = get_coco(root_path, data_type, None)
    data_loader = torch.utils.data.DataLoader(
        dataset, batch_size=2, shuffle=True,collate_fn=collate_fn)
    return data_loader

def train_one_epoch(model, optimizer,scheduler, data_loader, device, epoch, print_freq,num_warmup_step=0,updateBnFlag=True,decay_param=0.00001):
    model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    for images, targets in metric_logger.log_every(data_loader, print_freq, header):
        images = list(torchvision.transforms.functional.to_tensor(image).to(device) for image in images)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
        loss_dict = model(images, targets)

        losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())

        loss_value = losses_reduced.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        optimizer.zero_grad()
        losses.backward()
        if updateBnFlag and scheduler._step_count > num_warmup_step:
            updateBn(model,decay_param)
        optimizer.step()
        scheduler.step()

        metric_logger.update(loss=losses_reduced, **loss_dict_reduced)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])

    return metric_logger

def _get_iou_types(model):
    model_without_ddp = model
    if isinstance(model, torch.nn.parallel.DistributedDataParallel):
        model_without_ddp = model.module
    iou_types = ["bbox"]
    if isinstance(model_without_ddp, torchvision.models.detection.MaskRCNN):
        iou_types.append("segm")
    if isinstance(model_without_ddp, torchvision.models.detection.KeypointRCNN):
        iou_types.append("keypoints")
    return iou_types


@torch.no_grad()
def evaluate(model, data_loader, device):
    n_threads = torch.get_num_threads()
    # FIXME remove this and make paste_masks_in_image run on the GPU
    torch.set_num_threads(1)
    cpu_device = torch.device("cpu")
    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    coco = get_coco_api_from_dataset(data_loader.dataset)
    iou_types = _get_iou_types(model)
    coco_evaluator = CocoEvaluator(coco, iou_types)

    for images, targets in metric_logger.log_every(data_loader, 100, header):
        images = list(torchvision.transforms.functional.to_tensor(image).to(device) for image in images)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]


        model_time = time.time()
        outputs = model(images)

        outputs = [{k: v.to(cpu_device) for k, v in t.items()} for t in outputs]
        model_time = time.time() - model_time

        res = {target["image_id"].item(): output for target, output in zip(targets, outputs)}
        evaluator_time = time.time()
        coco_evaluator.update(res)
        evaluator_time = time.time() - evaluator_time
        metric_logger.update(model_time=model_time, evaluator_time=evaluator_time)


    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    coco_evaluator.synchronize_between_processes()

    # accumulate predictions from all images
    coco_evaluator.accumulate()
    coco_evaluator.summarize()
    torch.set_num_threads(n_threads)
    return coco_evaluator

def train(model,epochs,train_loader,val_loader=None,frequency=5):
    config = get_light_mask_rcnn_config()
    learning_rate = config['learning_rate']
    decay_param = config['decay_param']
    num_warmup_steps = config['num_warmup_steps']
    checkpoint_path = config['checkpoints']
    optimizer = AdamW(model.parameters(), lr=learning_rate, eps=1e-8)
    num_training_steps = len(train_loader) * epochs

    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=num_warmup_steps, num_training_steps=num_training_steps
    )

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    #device = torch.device("cpu")
    model.to(device)
    record = []
    for epoch in range(epochs):
        timestamp = date2str()
        metric_logger = train_one_epoch(model,optimizer,scheduler,train_loader,device,epoch,frequency,num_warmup_steps,True)
        record.append(metric_logger)
        if val_loader is not None:
            coco_evaluator = evaluate(model,val_loader,device)
            file_name = checkpoint_path + 'coco_evaluator_{0}.pkl'.format(timestamp)
            with open(file_name.format(timestamp),'wb') as f:
                 pickle.dump(coco_evaluator,f)
        if epoch % frequency == 0:
            save_checkpoint({
                'epoch': epoch,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'scheduler': scheduler.state_dict(),
            },checkpoint_path,timestamp=timestamp)
            file_name = checkpoint_path + 'loss_{0}.pkl'.format(timestamp)
            with open(file_name.format(timestamp),'wb') as f:
                 pickle.dump(record,f)
    file_name = checkpoint_path + "resnet_101.pth"
    torch.save(model.state_dict(), file_name)


if __name__ == "__main__":

    layers = [3, 4, 23, 3] #the number of bottlenet layers for Resnet 101
    root="C:\\Users\\multicampus\\Downloads\\coco_dataset"
    model = get_mask_rcnn_model(layers=layers,num_classes=91)
    train_loader = get_coco_dataloader(root_path=root, batch=1,Train=True)
    val_loader = get_coco_dataloader(root_path=root, batch=1, Train=True)
    train(model=model,epochs=40,train_loader=train_loader,val_loader=val_loader)

    #get pruned structure and train again from scratch
    pruned_cfg = get_pruned_config(model,0.4)
    model = get_mask_rcnn_model(layers,num_classes=91,cfg=pruned_cfg)
    train(model=model, epochs=40, train_loader=train_loader, val_loader=val_loader)



