"""
Entry points for compass
"""
from __future__ import absolute_import, print_function, division
import argparse
import os
import multiprocessing
import numpy as np
import pandas as pd
import sys
import subprocess as sp
import logging
import datetime
import json
import gzip
from functools import partial
from tqdm import tqdm
from six import string_types
from math import ceil

from .compass import cache
from ._version import __version__
from .compass.torque import submitCompassTorque
from .compass.algorithm import singleSampleCompass, maximize_reaction_range, maximize_metab_range, initialize_cplex_problem
from .models import init_model
from .compass.penalties import eval_reaction_penalties, compute_knn
from . import globals
from . import utils


def parseArgs():
    """Defines the command-line arguments and parses the Compass call

    Returns
    -------
    argparse.Namespace

    """
    parser = argparse.ArgumentParser(
                        prog="Compass",
                        description="Metabolic Modeling for Single Cells")

    parser.add_argument("--data", help="Gene expression matrix." 
                        "For .mtx inputs, it must be followed by a tsv with gene names"
                        "and optionally a list of cell barcodes",
                        nargs="+", 
                        metavar="FILES")

    parser.add_argument("--model", help="Metabolic Model to Use",
                        default="RECON2_mat",
                        choices=["RECON1_mat", "RECON2_mat", "RECON2.2"],
                        metavar="MODEL")

    parser.add_argument("--species",
                        help="Species to use to match genes to model",
                        choices=["homo_sapiens", "mus_musculus"],
                        metavar="SPECIES",
                        default="homo_sapiens")

    parser.add_argument("--media", help="Which media to simulate",
                        metavar="MEDIA")

    parser.add_argument("--output-dir", help="Where to store outputs",
                        default='.',
                        metavar="DIR")

    parser.add_argument("--temp-dir", help="Where to store temporary files",
                        default='<output-dir>/_tmp',
                        metavar="DIR")

    parser.add_argument("--torque-queue", help="Submit to a Torque queue",
                        metavar="QUEUE")

    parser.add_argument("--num-processes",
                        help="Limit to <N> Processes.  "
                             "Ignored when submitting job onto a queue",
                        type=int,
                        metavar="N")

    parser.add_argument("--lambda",
                        help="Smoothing factor for single-cell data. Should be"
                        " set between 0 and 1",
                        type=float,
                        default=0,
                        metavar="F")

    parser.add_argument("--single-sample",
                        help=argparse.SUPPRESS,
                        type=int,
                        metavar="N")

    parser.add_argument("--generate-cache",
                        help=argparse.SUPPRESS,
                        action="store_true")

    parser.add_argument("--test-mode",
                        help=argparse.SUPPRESS,
                        action="store_true")

    parser.add_argument("--num-threads",
                        help="Number of threads to use per sample",
                        type=int, default=1,
                        metavar="N")

    parser.add_argument(
        "--and-function",
        help="Which function used to aggregate AND associations",
        choices=["min", "median", "mean"],
        metavar="FXN",
        default="min")

    parser.add_argument(
        "--select_reactions",
        help="Compute compass scores only for the reactions listed in the given file. FILE is expected to be textual, with one line per reaction (undirected, namely adding the suffix \"_pos\" or \"_neg\" to a line will create a valid directed reaction id). Unrecognized reactions in FILE are ignored.",
        required=False,
        metavar="FILE")

    # Hidden argument.  Used for batch jobs
    parser.add_argument("--collect", action="store_true",
                        help=argparse.SUPPRESS)

    # Also used for batch jobs
    parser.add_argument("--config-file", help=argparse.SUPPRESS)

    parser.add_argument("--num-neighbors",
                        help="Either effective number of neighbors for "
                        "gaussian penalty diffusion or exact number of "
                        "neighbors for KNN penalty diffusion",
                        default=30,
                        type=int,
                        metavar="N")

    parser.add_argument("--symmetric-kernel", action="store_true",
                        help="Use symmetric TSNE kernel (slower)")

    parser.add_argument("--input-weights",
                        help="File with input sample to sample weights",
                        required=False, metavar="FILE")

    parser.add_argument("--penalty-diffusion",
                        help="Mode to use to share reaction penalty "
                        "values between single cells",
                        choices=["gaussian", "knn"],
                        metavar="MODE",
                        default="gaussian")

    parser.add_argument("--no-reactions", action="store_true",
                        help="Skip computing scores for reactions")

    parser.add_argument("--calc-metabolites", action="store_true",
                        help="Compute scores for metabolite "
                        "uptake/secretion")

    parser.add_argument("--precache", action="store_true",
                        help="Preprocesses the model to find "
                        " maximum fluxes")

    parser.add_argument("--input-knn", help="Path to search for a precomputed kNN",
                        default=None, metavar="KNN INPUT")

    parser.add_argument("--output-knn", help="File to save kNN of data to",
                        default=None, metavar="KNN OUTPUT")

    parser.add_argument("--latent-space", help="File with latent space for clustering",
                        default=None, metavar="LATENT")

    #Hidden argument which tracks more detailed information on runtimes
    parser.add_argument("--detailed-perf", action="store_true",
                        help=argparse.SUPPRESS)

    parser.add_argument("--penalties-file",
                        help=argparse.SUPPRESS,
                        default='')

    #Hidden argument to choose the algorithm CPLEX uses. Barrier generally best choice.
    #See - https://www.ibm.com/support/knowledgecenter/en/SS9UKU_12.10.0/com.ibm.cplex.zos.help/CPLEX/Parameters/topics/LPMETHOD.html
    parser.add_argument("--lpmethod",
                        help=argparse.SUPPRESS,
                        default=4,
                        type=int)

    #Hidden argument to choose the setting for Cplex's advanced basis setting. Generally 2 is the best, but for ease of testing I've added it here.
    parser.add_argument("--advance",
                        help=argparse.SUPPRESS,
                        default=2,
                        type=int)

    #Hidden argument to save argmaxes in the temp directory
    parser.add_argument("--save-argmaxes", action="store_true",
                        help=argparse.SUPPRESS)
                        

    #Argument to output the list of needed genes to a file
    parser.add_argument("--list-genes", default=None, help="File to output a list of metabolic genes needed for selected metabolic model.")

    args = parser.parse_args()

    args = vars(args)  # Convert to a Dictionary

    load_config(args)

    if not args['data']:
        if not args['precache'] and not args['list_genes']:
            parser.error("--data [file] required unless --precache or --list-genes option selected")
    else:
        args['data'] = [os.path.abspath(p) for p in args['data']]
        if len(args['data']) == 2:
            args['data'].append(None)

    if args['input_weights']:
        args['input_weights'] = os.path.abspath(args['input_weights'])

    if args['select_reactions']:
        args['select_reactions'] = os.path.abspath(args['select_reactions'])

    if args['temp_dir'] == "<output-dir>/_tmp":
        args['temp_dir'] = os.path.join(args['output_dir'], '_tmp')

    args['output_dir'] = os.path.abspath(args['output_dir'])
    args['temp_dir'] = os.path.abspath(args['temp_dir'])

    if args['input_knn']:
        args['input_knn'] = os.path.abspath(args['input_knn'])
    if args['output_knn']:
        args['output_knn'] = os.path.abspath(args['output_knn'])
    if args['latent_space']:
        args['latent_space'] = os.path.abspath(args['latent_space'])

    if args['lambda'] < 0 or args['lambda'] > 1:
        parser.error(
            "'lambda' parameter cannot be less than 0 or greater than 1"
        )

    if args['generate_cache'] and \
            (args['no_reactions'] or not args['calc_metabolites']):

        parser.error(
            "--generate-cache cannot be run with --no-reactions or "
            "without --calc-metabolites" #Not sure about why this needs metabolites to calculated
        )
    return args


def entry():
    """Entry point for the compass command-line script
    """
    
    start_time = datetime.datetime.now()

    args = parseArgs()

    if args['data']:
        if not os.path.isdir(args['output_dir']):
            os.makedirs(args['output_dir'])

        if not os.path.isdir(args['temp_dir']) and args['temp_dir'] != '/dev/null':
            os.makedirs(args['temp_dir'])

        globals.init_logger(args['output_dir'])
        

    # Log some things for debugging/record
    logger = logging.getLogger('compass')
    logger.debug("Compass version: " + __version__)

    try:
        commit = sp.check_output(
            ["git", '--git-dir', globals.GIT_DIR, "rev-parse", "--short",
             "HEAD"],
            stderr=open(os.devnull, 'w')
        )
        logger.debug("Git commit: " + commit.decode())
    except sp.CalledProcessError:
        logger.debug("Git commit: Not in Git repo")

    logger.debug("Python Version:")
    logger.debug(sys.version)
    logger.debug("Python prefix: " + sys.prefix)
    logger.debug("Numpy version: " + np.__version__)
    logger.debug("Pandas version: " + pd.__version__)
    logger.debug("Supplied Arguments: ")
    for (key, val) in args.items():
        logger.debug("   {}: {}".format(key, val))

    logger.debug("\nCOMPASS Started: {}".format(start_time))
    # Parse arguments and decide what course of action to take

    if args['list_genes'] is not None:
        model = init_model(model=args['model'], species=args['species'],
                exchange_limit=globals.EXCHANGE_LIMIT, media=args['media'])
        genes = list(set.union(*[set(reaction.list_genes()) for reaction in model.reactions.values()]))
        genes = "\n".join(genes)
        with open(args['list_genes'], 'w') as fout:
            fout.write(genes.encode('utf8'))
            fout.close()
        return
            

    #if args['output_knn']:
    #    compute_knn(args)
    #    logger.info("Compass computed knn succesfully")
    #    return 

    #Check if the cache for (model, media) exists already:
    size_of_cache = len(cache.load(init_model(model=args['model'], species=args['species'],
                    exchange_limit=globals.EXCHANGE_LIMIT,
                    media=args['media']), args['media']))
    if size_of_cache == 0 or args['precache']:
        logger.info("Building up model cache")
        precacheCompass(args=args)
        end_time = datetime.datetime.now()
        logger.debug("\nElapsed Time: {}".format(end_time-start_time))
        if not args['data']:
            return
    else:
        logger.info("Cache already built")

    if args['single_sample'] is not None:
        singleSampleCompass(data=args['data'], model=args['model'],
                            media=args['media'], directory=args['temp_dir'],
                            sample_index=args['single_sample'], args=args)
        end_time = datetime.datetime.now()
        logger.debug("\nElapsed Time: {}".format(end_time-start_time))
        return

    if args['collect']:
        collectCompassResults(args['data'], args['temp_dir'],
                              args['output_dir'], args)
        end_time = datetime.datetime.now()
        logger.debug("\nElapsed Time: {}".format(end_time-start_time))
        return

    # Time to evaluate the reaction expression
    success_token = os.path.join(args['temp_dir'], 'success_token_penalties')
    penalties_file = os.path.join(args['temp_dir'], 'penalties.txt.gz')
    if os.path.exists(success_token):
        logger.info("Reaction Penalties already evaluated")
        logger.info("Resuming execution from previous run...")
    else:
        logger.info("Evaluating Reaction Penalties...")
        penalties = eval_reaction_penalties(args['data'], args['model'],
                                            args['media'], args['species'],
                                            args)
        penalties.to_csv(penalties_file, sep='\t', compression='gzip')
        with open(success_token, 'w') as fout:
            fout.write('Success!')

    args['penalties_file'] = penalties_file

    # Now run the individual cells through cplex in parallel
    # This is either done by sending to Torque queue, or running on the
    # same machine
    if args['torque_queue'] is not None:
        logger.info(
            "Submitting COMPASS job to Torque queue - {}".format(
                args['torque_queue'])
        )
        submitCompassTorque(args,
                            temp_dir=args['temp_dir'],
                            output_dir=args['output_dir'],
                            queue=args['torque_queue'])
        return
    else:
        runCompassParallel(args)
        end_time = datetime.datetime.now()
        logger.debug("\nElapsed Time: {}".format(end_time-start_time))
        return

def runCompassParallel(args):

    logger = logging.getLogger('compass')

    # If we're here, then run compass on this machine with N processes
    if args['num_processes'] is None:
        args['num_processes'] = multiprocessing.cpu_count()

    if args['num_processes'] > multiprocessing.cpu_count():
        args['num_processes'] = multiprocessing.cpu_count()

    # Get the number of samples
    data = utils.read_data(args['data'])#pd.read_csv(args['data'], sep='\t', index_col=0)
    n_samples = len(data.columns)

    partial_map_fun = partial(_parallel_map_fun, args=args)

    pool = multiprocessing.Pool(args['num_processes'])

    logger.info(
        "Processing {} samples using {} processes"
        .format(n_samples, args['num_processes'])
    )

    logger.info(
        "Progress bar will update once the first sample is finished"
    )

    pbar = tqdm(total=n_samples)

    for _ in pool.imap_unordered(partial_map_fun, range(n_samples)):
        pbar.update()

    collectCompassResults(args['data'], args['temp_dir'],
                          args['output_dir'], args)

    logger.info("COMPASS Completed Successfully")


def _parallel_map_fun(i, args):

        data = args['data']
        model = args['model']
        media = args['media']
        temp_dir = args['temp_dir']

        sample_dir = os.path.join(temp_dir, 'sample' + str(i))

        if not os.path.isdir(sample_dir):
            os.makedirs(sample_dir)

        out_file = os.path.join(sample_dir, 'out.log')
        err_file = os.path.join(sample_dir, 'err.log')
        with open(out_file, 'w') as fout, open(err_file, 'w') as ferr:
            stdout_bak = sys.stdout
            stderr_bak = sys.stderr
            sys.stdout = fout
            sys.stderr = ferr

            globals.init_logger(sample_dir)

            logger = logging.getLogger('compass')
            logger.debug("Compass: Single-sample mode")
            logger.debug("Supplied Arguments: ")
            for (key, val) in args.items():
                logger.debug("   {}: {}".format(key, val))

            start_time = datetime.datetime.now()
            logger.debug("\nCOMPASS Started: {}".format(start_time))

            try:
                singleSampleCompass(
                    data=data, model=model,
                    media=media, directory=sample_dir,
                    sample_index=i, args=args
                )
            except Exception as e:
                sys.stdout = stdout_bak
                sys.stderr = stderr_bak

                # Necessary because cplex exceptions can't be pickled
                #   and can't transfer from subprocess to main process
                if 'cplex' in str(type(e)).lower():
                    raise(Exception(str(e)))
                else:
                    raise(e)

            end_time = datetime.datetime.now()
            logger.debug("\nElapsed Time: {}".format(end_time-start_time))


def collectCompassResults(data, temp_dir, out_dir, args):
    """
    Collects results for individual samples in temp_dir
    and aggregates into out_dir

    Parameters
    ==========
    data : str
       Full path to data file

    temp_dir : str
        Directory - where to look for sample results.

    out_dir : str
        Where to store aggregated results.  Is created if it doesn't exist.

    args : dict
        Other arguments
    """

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    logger = logging.getLogger('compass')
    logger.info("Collecting results from: " + temp_dir)
    logger.info("Writing output to: " + out_dir)

    # Get the number of samples
    expression = utils.read_data(data)#pd.read_csv(data, sep='\t', index_col=0)
    n_samples = len(expression.columns)

    reactions_all = []
    secretions_all = []
    uptake_all = []

    # Gather all the results
    for i in range(n_samples):

        sample_name = expression.columns[i]
        sample_dir = os.path.join(temp_dir, 'sample' + str(i))

        try:
            reactions = pd.read_csv(
                os.path.join(sample_dir, 'reactions.txt'),
                sep='\t', index_col=0)

            reactions_all.append(reactions)

        except:
            reactions_all.append(pd.DataFrame(columns=[sample_name]))

        try:
            secretions = pd.read_csv(
                os.path.join(sample_dir, 'secretions.txt'),
                sep='\t', index_col=0)

            secretions_all.append(secretions)

        except:
            secretions_all.append(pd.DataFrame(columns=[sample_name]))

        try:
            uptake = pd.read_csv(
                os.path.join(sample_dir, 'uptake.txt'),
                sep='\t', index_col=0)

            uptake_all.append(uptake)

        except:
            uptake_all.append(pd.DataFrame(columns=[sample_name]))

    # Join and output
    if not args['no_reactions']:
        reactions_all = pd.concat(reactions_all, axis=1, sort=True)
        reactions_all.to_csv(
            os.path.join(out_dir, 'reactions.tsv'), sep="\t")

    if args['calc_metabolites']:
        secretions_all = pd.concat(secretions_all, axis=1, sort=True)
        secretions_all.to_csv(
            os.path.join(out_dir, 'secretions.tsv'), sep="\t")

        uptake_all = pd.concat(uptake_all, axis=1, sort=True)
        uptake_all.to_csv(
            os.path.join(out_dir, 'uptake.tsv'), sep="\t")

    # Output a JSON version of the model
    model = init_model(model=args['model'], species=args['species'],
                       exchange_limit=globals.EXCHANGE_LIMIT,
                       media=args['media'])

    model_file = os.path.join(out_dir, 'model.json.gz')

    with gzip.open(model_file, 'w') as gzfile:
        gzfile.write(model.to_JSON().encode('utf-8'))

def load_config(args):
    """
    If a config file is specified, this loads the file
    and applies the arguments in the config file - overwriting
    other arguments specified at the command line

    Really just for batch jobs to make it easier to
    propagate arguments
    """

    if ("config_file" not in args or
            args["config_file"] is None):
        return

    filename = args["config_file"]
    with open(filename) as fin:
        newArgs = json.load(fin)

    # Cast all the newArgs using str
    # Fixes unicode issues on python2
    for key in newArgs:
        if isinstance(newArgs[key], string_types):
            newArgs[key] = str(newArgs[key])

    args.update(newArgs)

def _parallel_map_precache_reactions(start_stop, args):
    return maximize_reaction_range(start_stop, args)

def _parallel_map_precache_metabs(start_stop, args):
    return maximize_metab_range(start_stop, args)

def precacheCompass(args):

    logger = logging.getLogger('compass')

    if args['num_processes'] is None:
        args['num_processes'] = multiprocessing.cpu_count()
    if args['num_processes'] > multiprocessing.cpu_count():
        args['num_processes'] = multiprocessing.cpu_count()

    model = init_model(model=args['model'], species=args['species'],
        exchange_limit=globals.EXCHANGE_LIMIT, media=args['media'])

    n_processes = args['num_processes'] #max(1, args['num_processes'] - 1) #for later multithreading
    n_reactions = len(model.reactions.values())
    chunk_size = int(ceil(n_reactions / n_processes))
    chunks = [(i*chunk_size, min(n_reactions, (i+1)*chunk_size)) for i in range(n_processes)]

    combined_cache = {}
    partial_map_fun = partial(_parallel_map_precache_reactions, args=args)
    pool = multiprocessing.Pool(n_processes)
    for sub_cache in pool.imap_unordered(partial_map_fun, chunks):
        combined_cache.update(sub_cache)

    problem = initialize_cplex_problem(model, args['num_threads'], args['lpmethod'])
    n_metabs = len(problem.linear_constraints.get_names())
    chunk_size = int(ceil(n_metabs / n_processes))
    chunks = [(i*chunk_size, min(n_metabs, (i+1)*chunk_size)) for i in range(n_processes)]

    partial_map_fun = partial(_parallel_map_precache_metabs, args=args)
    pool = multiprocessing.Pool(n_processes)
    for sub_cache in pool.imap_unordered(partial_map_fun, chunks):
        combined_cache.update(sub_cache)

    #cache.clear(model) TBD? don't want all the time
    model_cache = cache.load(model)
    model_cache.update(combined_cache)
    #model_cache['dfs_reaction_order'] = lst
    cache.save(model) 
    