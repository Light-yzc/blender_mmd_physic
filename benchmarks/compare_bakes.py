"""Compare two JSON exports from bake_performance.py, including guard keys."""
import argparse
import json
from pathlib import Path


def compare(before, after):
    if before['range'] != after['range'] or before['preroll'] != after['preroll']:
        raise ValueError('Frame range and preroll must match')
    identical, compared, max_error, shape_errors = True, 0, 0.0, []
    if before['curves'].keys() != after['curves'].keys():
        shape_errors.append('armatures')
    for arm, curves in before['curves'].items():
        other = after['curves'].get(arm, {})
        if curves.keys() != other.keys():
            shape_errors.append(arm + ': channels')
        for key, values in curves.items():
            result = other.get(key, [])
            if len(values) != len(result):
                shape_errors.append(arm + ': ' + key)
                continue
            for left, right in zip(values, result):
                compared += 1
                identical &= left == right
                if left[0] != right[0] or left[2] != right[2]:
                    shape_errors.append(arm + ': key frame/interpolation')
                max_error = max(max_error, abs(left[1] - right[1]))
    return {'before_seconds': before['seconds'], 'after_seconds': after['seconds'],
            'speedup': before['seconds'] / after['seconds'],
            'time_reduction_percent': (1 - after['seconds'] / before['seconds']) * 100,
            'before_peak_rss_mib': before['peak_rss_mib'],
            'after_peak_rss_mib': after['peak_rss_mib'],
            'compared_key_records': compared, 'exact_match': bool(identical and not shape_errors),
            'max_value_error': max_error, 'shape_errors': shape_errors[:10]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    args = parser.parse_args()
    result = compare(json.loads(args.before.read_text()), json.loads(args.after.read_text()))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['exact_match'] else 1)
