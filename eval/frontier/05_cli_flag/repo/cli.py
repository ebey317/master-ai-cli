"""CLI parser."""

import argparse


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    return parser
