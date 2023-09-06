
import os
from typing import Final

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.axes import Axes
from matplotlib.ticker import MaxNLocator

from narlsqr.analysis import MetricsAnalyzer

RESULTS_DIR: Final = 'data/results'
ANALYSIS_DIR: Final = 'data/analysis'
DEVICES: Final = ['manila', 'belem', 'nairobi', 'guadalupe', 'mumbai']


def format_plot(ax: Axes, x_label: str, y_label: str, y_ticks: int = 16):
    ax.tick_params(labelsize=23)
    ax.get_yaxis().set_major_locator(MaxNLocator(nbins=y_ticks))
    ax.set_xlabel(x_label, labelpad=8.0, fontsize=27, fontweight='bold')
    ax.set_ylabel(y_label, labelpad=8.0, fontsize=27, fontweight='bold')
    ax.get_figure().set_size_inches(14.0, 14.0)

def save_current_plot(path: str):
    plt.savefig(path, bbox_inches='tight', pad_inches=0.2)
    plt.close()


def random_circuits_analysis(device: str):
    prefix = f'{ANALYSIS_DIR}/{device}/random'
    os.makedirs(prefix, exist_ok=True)

    metrics_analyzer = MetricsAnalyzer.unpickle(f'{RESULTS_DIR}/{device}/random.pickle')
    noise_unaware = MetricsAnalyzer.unpickle(f'{RESULTS_DIR}/{device}/random_nu.pickle')
    metrics_analyzer.metrics['rl_noise_unaware'] = noise_unaware.metrics['rl']

    for metric in ['added_cnot_count', 'depth', 'log_reliability', 'reliability']:
        df = metrics_analyzer.metric_as_df(metric)

        float_format = '.3f' if metric in {'log_reliability', 'reliability'} else '.2f'
        mean = [f'{x:{float_format}}' for x in df.mean()]
        std = [f'{x:{float_format}}' for x in df.std()]

        with open(f'{prefix}/{metric}.txt', 'w') as f:
            f.write(f'Mean: {" & ".join(mean)}\n')
            f.write(f'Std: {" & ".join(std)}\n')

    ax = metrics_analyzer.box_plot('added_cnot_count')
    format_plot(ax, 'Routing Algorithm', 'Additional CNOT Gates')
    save_current_plot(f'{prefix}/added_cnot_count.pdf')

    ax = metrics_analyzer.box_plot('depth')
    format_plot(ax, 'Routing Algorithm', 'Circuit Depth')
    save_current_plot(f'{prefix}/depth.pdf')

    ax = metrics_analyzer.box_plot('log_reliability')
    format_plot(ax, 'Routing Algorithm', 'Log Reliability')
    save_current_plot(f'{prefix}/log_reliability.pdf')

def real_circuits_analysis(device: str):
    prefix = f'{ANALYSIS_DIR}/{device}/real'
    os.makedirs(prefix, exist_ok=True)

    metrics_analyzer = MetricsAnalyzer.unpickle(f'{RESULTS_DIR}/{device}/real.pickle')
    noise_unaware = MetricsAnalyzer.unpickle(f'{RESULTS_DIR}/{device}/real_nu.pickle')
    metrics_analyzer.metrics['rl_noise_unaware'] = noise_unaware.metrics['rl']

    for metric in ['normalized_added_cnot_count', 'normalized_depth', 'normalized_log_reliability']:
        df = metrics_analyzer.metric_as_df(metric)

        float_format = '.4f' if metric == 'normalized_log_reliability' else '.3f'
        mean = [f'{x:{float_format}}' for x in df.mean()]
        std = [f'{x:{float_format}}' for x in df.std()]

        with open(f'{prefix}/{metric.removeprefix("normalized_")}.txt', 'w') as f:
            f.write(f'Mean: {" & ".join(mean)}\n')
            f.write(f'Std: {" & ".join(std)}\n')

    ax = metrics_analyzer.box_plot('normalized_added_cnot_count')
    ax.tick_params(labelsize=16)
    format_plot(ax, 'Routing Algorithm', 'Additional CNOT Gates (Normalized)')
    save_current_plot(f'{prefix}/added_cnot_count.pdf')

    ax = metrics_analyzer.box_plot('normalized_depth')
    format_plot(ax, 'Routing Algorithm', 'Depth (Normalized)')
    save_current_plot(f'{prefix}/depth.pdf')

    ax = metrics_analyzer.box_plot('normalized_log_reliability')
    format_plot(ax, 'Routing Algorithm', 'Log Reliability (Normalized)')
    save_current_plot(f'{prefix}/log_reliability.pdf')


def swap_vs_bridge():
    prefix = ANALYSIS_DIR
    os.makedirs(prefix, exist_ok=True)

    data = {}

    for device in DEVICES:
        metrics_analyzer = MetricsAnalyzer.unpickle(f'{RESULTS_DIR}/{device}/random.pickle')

        for action in ['swap', 'bridge']:
            mean = metrics_analyzer.metric_as_df(f'{action}_count')['rl'].mean()

            data.setdefault('count', []).append(mean)
            data.setdefault('action', []).append(action.upper())
            data.setdefault('device', []).append(device.capitalize())

    df = pd.DataFrame(data)
    cross_tab = pd.crosstab(
        index=df['device'],
        columns=df['action'],
        values=df['count'],
        aggfunc='sum',
        normalize='index',  # type: ignore
    )

    order = [device.capitalize() for device in DEVICES]

    ax: Axes = cross_tab.loc[order].plot(kind='bar', stacked=True)
    format_plot(ax, 'Device', 'Proportion', y_ticks=10)

    for n, x in enumerate([*cross_tab.loc[order].index.values]):
        for proportion, y_loc in zip(cross_tab.loc[x], cross_tab.loc[x].cumsum()):
            plt.text(
                x=n - 0.20,
                y=(y_loc - proportion) + (proportion / 2),
                s=f'{np.round(proportion * 100, 1)}%',
                color='white',
                fontsize=20,
                fontweight='bold',
            )

    ax.legend(
        loc='upper right',
        fontsize=20,
        title='Action',
        title_fontproperties={'size': 20, 'weight': 'bold'},
    )
    ax.set_ybound(upper=1.0)
    plt.xticks(rotation=0)
    ax.get_figure().set_size_inches(13.0, 10.0)

    save_current_plot(f'{prefix}/swap_vs_bridge.pdf')


def evaluation_episodes_analysis():
    prefix = ANALYSIS_DIR
    os.makedirs(prefix, exist_ok=True)
    episodes_list = [1, 2, 4, 8, 16]

    results_prefix = f'{RESULTS_DIR}/nairobi/episodes'

    metrics_analyzer = MetricsAnalyzer.unpickle(f'{results_prefix}/deterministic.pickle')
    metrics = metrics_analyzer.metrics

    metrics.pop('stochastic')
    metrics.pop('basic')

    metrics['deterministic'] = metrics.pop('rl')

    for num_episodes in episodes_list:
        name = f'stochastic_{num_episodes}ep'
        stochastic = MetricsAnalyzer.unpickle(f'{results_prefix}/{name}.pickle')
        metrics[name] = stochastic.metrics['rl']

    df = metrics_analyzer.metric_as_df('log_reliability')
    rename_map = dict(
        deterministic='Deterministic',
        **{f'stochastic_{n}ep': f'{n} Ep.' for n in episodes_list},
        sabre='SABRE',
    )
    df = df.reindex(columns=rename_map)
    df.rename(columns=rename_map, inplace=True)

    palette = sns.color_palette('flare', n_colors=len(episodes_list))
    palette.insert(0, (0.58, 0.76, 0.42))
    palette.append((0.26, 0.56, 0.86))

    ax = sns.boxplot(df, palette=palette)
    format_plot(ax, 'Routing Method', 'Log Reliability')
    ax.get_figure().set_size_inches(14.0, 11.0)

    save_current_plot(f'{prefix}/evaluation_episodes.pdf')


def routing_time():
    times_rl = []
    times_sabre = []

    for device in DEVICES:
        metrics_analyzer = MetricsAnalyzer.unpickle(f'{RESULTS_DIR}/{device}/random.pickle')
        df = metrics_analyzer.metric_as_df('routing_time')

        times_rl.append(f'{df["rl"].mean():.3f}')
        times_sabre.append(f'{df["sabre"].mean():.5f}')

    with open(f'{ANALYSIS_DIR}/routing_time.txt', 'w') as f:
        f.write(' & '.join(times_rl))
        f.write('\n')
        f.write(' & '.join(times_sabre))


def enhancements_analysis():
    prefix = ANALYSIS_DIR
    os.makedirs(prefix, exist_ok=True)

    metrics_analyzer = MetricsAnalyzer.unpickle(f'{RESULTS_DIR}/belem/random.pickle')

    metrics = metrics_analyzer.metrics
    metrics.pop('stochastic')
    metrics.pop('basic')

    variants = {
        'no_bridge': 'No BRIDGE\nGate',
        'no_embeddings': 'No\nEmbeddings',
        'no_front_layer_swaps': 'No SWAP\nRestrictions',
        'no_commutation': 'No\nCommutation\nAnalysis',
        'no_enhancements': 'No\nEnhancements',
    }

    for variant in variants:
        path = f'{RESULTS_DIR}/belem/enhancements/{variant}.pickle'
        metrics[variant] = MetricsAnalyzer.unpickle(path).metrics['rl']

    df = metrics_analyzer.metric_as_df('log_reliability')
    df.rename(columns=dict(rl='Default', **variants, sabre='SABRE'), inplace=True)
    df = df.reindex(df.mean().sort_values(ascending=False).index, axis=1)

    palette = sns.color_palette('flare', n_colors=len(variants) + 1)
    palette.append((0.26, 0.56, 0.86))

    ax = sns.boxplot(df, palette=palette)
    format_plot(ax, 'Routing Method', 'Log Reliability')
    ax.get_figure().set_size_inches(20.0, 13.0)

    save_current_plot(f'{prefix}/enhancements_analysis.pdf')


def main():
    sns.set_theme(style='whitegrid')
    plt.rcParams['font.sans-serif'] = ['Nimbus Sans']

    for device in DEVICES:
        random_circuits_analysis(device)
        real_circuits_analysis(device)

    swap_vs_bridge()
    evaluation_episodes_analysis()
    routing_time()
    enhancements_analysis()


if __name__ == '__main__':
    main()
