import pandas as pd


def concat_dataframes(frames, *args, preserve_columns=True, **kwargs):
    """Concatenate frames without pandas' empty/all-NA dtype FutureWarning.

    Pandas is changing how empty or all-NA DataFrames influence result dtypes.
    The dashboard wants the old behavior, so concat only the non-empty,
    non-all-NA columns, then restore the original column set for callers that
    rely on a stable schema.
    """
    frame_list = [frame for frame in frames if frame is not None]
    if not frame_list:
        return pd.DataFrame()

    axis = kwargs.get('axis', 0)
    if args or axis not in (0, 'index') or not all(isinstance(frame, pd.DataFrame) for frame in frame_list):
        return pd.concat(frame_list, *args, **kwargs)

    columns = []
    for frame in frame_list:
        for column in frame.columns:
            if column not in columns:
                columns.append(column)

    cleaned_frames = []
    for frame in frame_list:
        if frame.empty:
            continue
        non_all_na_columns = frame.columns[~frame.isna().all(axis=0)]
        cleaned_frames.append(frame.loc[:, non_all_na_columns].copy())

    if not cleaned_frames:
        return pd.DataFrame(columns=columns)

    result = pd.concat(cleaned_frames, **kwargs)
    if preserve_columns:
        for column in columns:
            if column not in result.columns:
                result[column] = pd.NA
        result = result.loc[:, columns]

    return result
