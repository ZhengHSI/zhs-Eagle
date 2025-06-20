import argparse

import os
from concurrent.futures import ThreadPoolExecutor

parser = argparse.ArgumentParser()
parser.add_argument("--outdir", type=str, default="/workdir/datasets/ShareGPT_Vicuna_unfiltered")
parser.add_argument("--dataset", type=str, default="/workdir/huggingface.co/datasets/Aeala/ShareGPT_Vicuna_unfiltered") #Aeala/ShareGPT_Vicuna_unfiltered
args = parser.parse_args()

s = 0
gpus = [[0], [1], [2], [3], [4], [5], [6], [7]]
num_p = len(gpus)

if "sharegpt" in args.dataset.lower():
    e = 120600 - 1
    outdir = "{}/ShareGPT_{}_{}_mufp16".format(args.outdir, s, e)
else:
    e = 200000 - 1
    outdir = "{}/ultrachat_{}_{}_mufp16".format(args.outdir, s, e)
    


def split_range(start, end, n, over=False):
    length = end - start + 1  # Include the end
    base_interval = length // n
    additional = length % n  # Get the remainder of the division
    intervals = []
    previous = start

    for i in range(n):
        current_interval = base_interval + (1 if i < additional else 0)
        if over:
            intervals.append((previous, previous + current_interval))
        else:
            intervals.append(
                (previous, previous + current_interval - 1)
            )  # '-1' because the end is inclusive
        previous += current_interval

    return intervals


def run_command(cmd):
    os.system(cmd)


if not os.path.exists(outdir):
    os.makedirs(outdir)


data_a = split_range(s, e, num_p, over=True)
commands = []
for i in range(num_p):
    index = i
    start = data_a[i][0]
    end = data_a[i][1]

    gpu_index = gpus[i]
    gpu_index_str = " ".join(map(str, gpu_index))
    command = "python3 generate_data.py --start={} --end={} --index={} --gpu_index {} --outdir {} --dataset {}".format(
        start, end, index, gpu_index_str, outdir, args.dataset
    )
    commands.append(command)

with ThreadPoolExecutor(max_workers=len(commands)) as executor:
    for command in commands:
        executor.submit(run_command, command)
        print(command)
