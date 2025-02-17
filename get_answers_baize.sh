#!/bin/bash

#SBATCH --job-name=get_answers_baize
#SBATCH --nodes=1
#SBATCH --gres=gpu:1
#SBATCH --time=12:00:00
#SBATCH --mem=16000MB
#SBATCH --cpus-per-task=8

source /home/${USER}/.bashrc
conda activate legal-master

srun python get_answers.py --adapter_1_dir /home/sojungkim2/legalmaster/LegalMaster/ChatAdapterTraining/baize_lora_v1