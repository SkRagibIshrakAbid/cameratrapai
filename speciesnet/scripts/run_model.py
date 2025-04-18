# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Script to run the SpeciesNet model.

Provides a command-line interface to execute the SpeciesNet model on various inputs. It
uses flags for specifying input, output, and run options, allowing the user to run the
model in different modes.
"""

import json
import multiprocessing as mp
from pathlib import Path
from typing import Callable, Literal, Optional

from absl import app
from absl import flags

from speciesnet import DEFAULT_MODEL
from speciesnet import only_one_true
from speciesnet import SpeciesNet
from speciesnet.ensemble_prediction_combiner import PredictionType
from speciesnet.utils import load_partial_predictions
from speciesnet.utils import prepare_instances_dict

#custom imports for ragib
import os
from PIL import Image, ImageDraw, ImageFont, ImageColor
from matplotlib import font_manager
import cv2

_MODEL = flags.DEFINE_string(
    "model",
    DEFAULT_MODEL,
    "SpeciesNet model to load.",
)
_CLASSIFIER_ONLY = flags.DEFINE_bool(
    "classifier_only",
    False,
    "Run only the classifier component. --classifier_only enables classifier-only mode, --noclassifier_only (default) disables it.",
)
_DETECTOR_ONLY = flags.DEFINE_bool(
    "detector_only",
    False,
    "Run only the detector component. --detector_only enables detector-only mode, --nodetector_only (default) disables it.",
)
_ENSEMBLE_ONLY = flags.DEFINE_bool(
    "ensemble_only",
    False,
    "Run only the ensemble component. --ensemble_only enables ensemble-only mode, --noensemble_only (default) disables it.",
)
_GEOFENCE = flags.DEFINE_bool(
    "geofence",
    True,
    "Enable geofencing during ensemble prediction. --geofence (default) enables geofencing, --nogeofence disables it.",
)
_INSTANCES_JSON = flags.DEFINE_string(
    "instances_json",
    None,
    "Input JSON file with instances to get predictions for.",
)
_FILEPATHS = flags.DEFINE_list(
    "filepaths",
    None,
    "List of image filepaths to get predictions for.",
)
_FILEPATHS_TXT = flags.DEFINE_string(
    "filepaths_txt",
    None,
    "Input TXT file with image filepaths to get predictions for.",
)
_FOLDERS = flags.DEFINE_list(
    "folders",
    None,
    "List of image folders to get predictions for.",
)
_FOLDERS_TXT = flags.DEFINE_string(
    "folders_txt",
    None,
    "Input TXT file with image folders to get predictions for.",
)
_COUNTRY = flags.DEFINE_string(
    "country",
    None,
    "Country (in ISO 3166-1 alpha-3 format, e.g. 'AUS') to enforce on all instances.",
)
_ADMIN1_REGION = flags.DEFINE_string(
    "admin1_region",
    None,
    "First-level administrative division (in ISO 3166-2 format, e.g. 'CA') to enforce on all "
    "instances.",
)
_TARGET_SPECIES_TXT = flags.DEFINE_string(
    "target_species_txt",
    None,
    "Input TXT file with species of interest to always compute classification scores for.",
)
_CLASSIFICATIONS_JSON = flags.DEFINE_string(
    "classifications_json",
    None,
    "Input JSON file with classifications from previous runs.",
)
_DETECTIONS_JSON = flags.DEFINE_string(
    "detections_json",
    None,
    "Input JSON file with detections from previous runs.",
)
_PREDICTIONS_JSON = flags.DEFINE_string(
    "predictions_json",
    None,
    "Output JSON file for storing computed predictions. If this file exists, only instances "
    "that are not already present in the output will be processed.",
)
_RUN_MODE = flags.DEFINE_enum(
    "run_mode",
    "multi_thread",
    ["multi_thread", "multi_process"],
    "Parallelism strategy.",
)
_BATCH_SIZE = flags.DEFINE_integer(
    "batch_size",
    8,
    "Batch size for classifier inference.",
)
_PROGRESS_BARS = flags.DEFINE_bool(
    "progress_bars",
    True,
    "Whether to show progress bars for the various inference components. --progress_bars "
    "(default) enables progress bars, --noprogress_bars disables them.",
)
_BYPASS_PROMPTS = flags.DEFINE_bool(
    "bypass_prompts",
    False,
    "Whether to bypass confirmation prompts when expected files aren't supplied, or "
    "unexpected files are supplied. --bypass_prompts bypasses prompts, --nobypass_prompts "
    "(default) does not.",
)


def guess_predictions_source(
    predictions: dict[str, dict],
) -> Literal["classifier", "detector", "ensemble", "unknown", "invalid"]:
    """Guesses which model component generated given predictions.

    Args:
        predictions: Dict of predictions, keyed by filepaths.

    Returns:
        Returns "classifier", "detector" or "ensemble" when the corresponding component
        was identified as the source of predictions. Returns "invalid" when predictions
        contain both classifications and detections, but couldn't identify results from
        the ensemble. Returns "unknown" when no prediction is recognizable (e.g. when
        there are only failures).
    """

    found_classifications = False
    found_detections = False
    found_ensemble_results = False

    for prediction in predictions.values():
        if "classifications" in prediction:
            found_classifications = True
        if "detections" in prediction:
            found_detections = True
        if "prediction" in prediction:
            found_ensemble_results = True
        if found_classifications and found_detections and not found_ensemble_results:
            return "invalid"

    if found_ensemble_results:
        return "ensemble"
    if found_classifications:
        return "classifier"
    if found_detections:
        return "detector"
    return "unknown"


def custom_combine_predictions_fn(
    *,
    classifications: dict[str, list],
    detections: list[dict],
    country: Optional[str],
    admin1_region: Optional[str],
    taxonomy_map: dict,
    geofence_map: dict,
    enable_geofence: bool,
    geofence_fn: Callable,
    roll_up_fn: Callable,
) -> PredictionType:
    """Ensembles classifications and detections in a custom way.

    Args:
        classifications:
            Dict of classification results. "classes" and "scores" are expected to be
            provided among the dict keys.
        detections:
            List of detection results, sorted in decreasing order of their confidence
            score. Each detection is expected to be a dict providing "label" and "conf"
            among its keys.
        country:
            Country (in ISO 3166-1 alpha-3 format) associated with predictions.
            Optional.
        admin1_region:
            First-level administrative division (in ISO 3166-2 format) associated with
            predictions. Optional.
        taxonomy_map:
            Dictionary mapping taxa to labels.
        geofence_map:
            Dictionary mapping full class strings to geofence rules.
        enable_geofence:
            Whether geofencing is enabled.
        geofence_fn:
            Callable to geofence animal classifications.
        roll_up_fn:
            Callable to roll up labels to the first matching level.

    Returns:
        A tuple of <label, score, prediction_source> describing the ensemble result.
    """

    del detections  # Unused.
    del country  # Unused.
    del admin1_region  # Unused.
    del taxonomy_map  # Unused.
    del geofence_map  # Unused.
    del enable_geofence  # Unused.
    del geofence_fn  # Unused.
    del roll_up_fn  # Unused.

    # Always return the second classifier prediction.
    return (
        classifications["classes"][1],
        classifications["scores"][1],
        "custom_ensemble",
    )


def say_yes_to_continue(question: str, stop_message: str) -> bool:
    if _BYPASS_PROMPTS.value:
        return True
    user_input = input(f"{question} [y/N]: ")
    if user_input.lower() in ["yes", "y"]:
        return True
    else:
        print(stop_message)
        return False


def local_file_exists(filepath: Optional[str]) -> bool:
    if not filepath:
        return False
    return Path(filepath).exists()


def main(argv: list[str]) -> None:
    del argv  # Unused.

    # Check for a valid combination of components to run.
    components = [_CLASSIFIER_ONLY, _DETECTOR_ONLY, _ENSEMBLE_ONLY]
    components_names = [f"--{c.name}" for c in components]
    components_values = [c.value for c in components]
    components_strings = [
        f"{name}={value}" for name, value in zip(components_names, components_values)
    ]
    if any(components_values) and not only_one_true(*components_values):
        raise ValueError(
            f"Expected at most one of [{', '.join(components_names)}] to be provided. "
            f"Received: [{', '.join(components_strings)}]."
        )
    if _ENSEMBLE_ONLY.value and (
        not _CLASSIFICATIONS_JSON.value or not _DETECTIONS_JSON.value
    ):
        raise ValueError(
            f"Expected --{_CLASSIFICATIONS_JSON.name} and --{_DETECTIONS_JSON.name} to "
            f"be set when --{_ENSEMBLE_ONLY.name} is requested."
        )
    if _CLASSIFIER_ONLY.value:
        components = "classifier"
    elif _DETECTOR_ONLY.value:
        components = "detector"
    elif _ENSEMBLE_ONLY.value:
        components = "ensemble"
    else:
        components = "all"

    # Check for valid inputs.
    inputs = [_INSTANCES_JSON, _FILEPATHS, _FILEPATHS_TXT, _FOLDERS, _FOLDERS_TXT]
    inputs_names = [f"--{i.name}" for i in inputs]
    inputs_values = [i.value for i in inputs]
    inputs_strings = [
        f"{name}={value}" for name, value in zip(inputs_names, inputs_values)
    ]
    if not only_one_true(*inputs_values):
        raise ValueError(
            f"Expected exactly one of [{', '.join(inputs_names)}] to be provided. "
            f"Received: [{', '.join(inputs_strings)}]."
        )
    instances_dict = prepare_instances_dict(
        instances_json=_INSTANCES_JSON.value,
        filepaths=_FILEPATHS.value,
        filepaths_txt=_FILEPATHS_TXT.value,
        folders=_FOLDERS.value,
        folders_txt=_FOLDERS_TXT.value,
        country=_COUNTRY.value,
        admin1_region=_ADMIN1_REGION.value,
    )

    # Check the compatibility of output predictions with existing partial predictions.
    if _PREDICTIONS_JSON.value:
        partial_predictions, _ = load_partial_predictions(
            _PREDICTIONS_JSON.value, instances_dict["instances"]
        )
        predictions_source = guess_predictions_source(partial_predictions)

        if _CLASSIFIER_ONLY.value and predictions_source not in [
            "classifier",
            "unknown",
        ]:
            raise RuntimeError(
                f"The classifier risks overwriting previous predictions from "
                f"`{_PREDICTIONS_JSON.value}` that were produced by different "
                f"components. Make sure to provide a different output location to "
                f"--{_PREDICTIONS_JSON.name}."
            )

        if _DETECTOR_ONLY.value and predictions_source not in ["detector", "unknown"]:
            raise RuntimeError(
                f"The detector risks overwriting previous predictions from "
                f"`{_PREDICTIONS_JSON.value}` that were produced by different "
                f"components. Make sure to provide a different output location to "
                f"--{_PREDICTIONS_JSON.name}."
            )

        if _ENSEMBLE_ONLY.value and predictions_source not in ["ensemble", "unknown"]:
            raise RuntimeError(
                f"The ensemble risks overwriting previous predictions from "
                f"`{_PREDICTIONS_JSON.value}` that were produced by different "
                f"components. Make sure to provide a different output location to "
                f"--{_PREDICTIONS_JSON.name}."
            )

    else:
        if not say_yes_to_continue(
            question="Continue without saving predictions to a JSON file?",
            stop_message=(
                f"Please provide an output filepath via --{_PREDICTIONS_JSON.name}."
            ),
        ):
            return

    # If a list of target species is given, check that it exists
    if _TARGET_SPECIES_TXT.value is not None and not local_file_exists(
        _TARGET_SPECIES_TXT.value
    ):
        raise RuntimeError(
            f"Target species file '{_TARGET_SPECIES_TXT.value}' specified via --{_PREDICTIONS_JSON.name} does not exist."
        )

    # Load classifications and/or detections from previous runs.
    classifications_dict, _ = load_partial_predictions(
        _CLASSIFICATIONS_JSON.value, instances_dict["instances"]
    )
    detections_dict, _ = load_partial_predictions(
        _DETECTIONS_JSON.value, instances_dict["instances"]
    )

    # Set running mode.
    run_mode = _RUN_MODE.value
    mp.set_start_method("spawn")

    # Make predictions.
    model = SpeciesNet(
        _MODEL.value,
        components=components,
        geofence=_GEOFENCE.value,
        target_species_txt=_TARGET_SPECIES_TXT.value,
        # Uncomment the line below if you want to run your own custom ensembling
        # routine. And also, implement that routine! :-)
        # combine_predictions_fn=custom_combine_predictions_fn,
        multiprocessing=(run_mode == "multi_process"),
    )
    if hasattr(model, "classifier") and not hasattr(model, "detector"):
        if (
            model.classifier.model_info.type_ == "always_crop"
            and not local_file_exists(_DETECTIONS_JSON.value)
        ):
            if not say_yes_to_continue(
                question=(
                    "Classifier expects detections JSON. Continue without providing "
                    "this file and run classifier on full images instead of crops?"
                ),
                stop_message=(
                    f"Please provide detections via --{_DETECTIONS_JSON.name} and make "
                    "sure that file exists."
                ),
            ):
                return
        elif (
            model.classifier.model_info.type_ == "full_image" and _DETECTIONS_JSON.value
        ):
            if not say_yes_to_continue(
                question=(
                    "Classifier doesn't expect detections JSON, yet such file was "
                    f"provided via --{_DETECTIONS_JSON.name}. Continue even though "
                    "given detections JSON will have no effect?"
                ),
                stop_message=f"Please drop the --{_DETECTIONS_JSON.name} flag.",
            ):
                return
    if _CLASSIFIER_ONLY.value:
        predictions_dict = model.classify(
            instances_dict=instances_dict,
            detections_dict=detections_dict,
            run_mode=run_mode,
            batch_size=_BATCH_SIZE.value,
            progress_bars=_PROGRESS_BARS.value,
            predictions_json=_PREDICTIONS_JSON.value,
        )
    elif _DETECTOR_ONLY.value:
        predictions_dict = model.detect(
            instances_dict=instances_dict,
            run_mode=run_mode,
            progress_bars=_PROGRESS_BARS.value,
            predictions_json=_PREDICTIONS_JSON.value,
        )
    elif _ENSEMBLE_ONLY.value:
        predictions_dict = model.ensemble_from_past_runs(
            instances_dict=instances_dict,
            classifications_dict=classifications_dict,
            detections_dict=detections_dict,
            progress_bars=_PROGRESS_BARS.value,
            predictions_json=_PREDICTIONS_JSON.value,
        )
    else:
        predictions_dict = model.predict(
            instances_dict=instances_dict,
            run_mode=run_mode,
            batch_size=_BATCH_SIZE.value,
            progress_bars=_PROGRESS_BARS.value,
            predictions_json=_PREDICTIONS_JSON.value,
        )
    if predictions_dict is not None:
        print(
            "Predictions:\n"
            + json.dumps(predictions_dict, ensure_ascii=False, indent=4)
        )
    
    #______________________________________________________________________________________________________________________
    #print("Testing directory")
    #print(os.path.dirname(_PREDICTIONS_JSON.value[0]))
    #print(os.path.dirname(_PREDICTIONS_JSON.value))
    # Get the directory of the script/exe
    #exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)

    # Define paths
    json_path = _PREDICTIONS_JSON.value
    output_folder = os.path.dirname(json_path)
    not_bear_folder = os.path.join(output_folder, "not_bear")

    # Load JSON file
    with open(json_path, "r") as file:
        data = json.load(file)

    # Create output directories
    os.makedirs(output_folder, exist_ok=True)
    os.makedirs(not_bear_folder, exist_ok=True)

    # Define font properties
    font_prop = font_manager.FontProperties(family="sans serif", weight="bold")
    font = ImageFont.truetype(font_manager.findfont(font_prop), size=12)
    border_size = 3

    # Process each prediction
    for prediction in data["predictions"]:
        img_path = prediction["filepath"]
        detections = prediction["detections"]
        prediction_text = prediction["prediction"]
        pred_score = prediction["prediction_score"]

        # Determine label
        if "ursus species" in prediction_text.lower():
            prediction_label = "bear"
        elif "bear family" in prediction_text.lower():
            prediction_label = "bear"
        elif "asiatic black bear" in prediction_text.lower():
            prediction_label = "bear"
        elif "brown bear" in prediction_text.lower():
            prediction_label = "bear"
        elif "cervidae" in prediction_text.lower():
            prediction_label = "deer"
        else:
            prediction_label = prediction_text.split(";")[-1]

        bear_detected = prediction_label == "bear"

        # Load image
        img = Image.open(img_path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        # Filter detections
        valid_detections = [d for d in detections if d["conf"] >= 0.8]

        # If no valid detections, keep the highest one
        if not valid_detections and detections:
            best_detection = max(detections, key=lambda x: x["conf"])
            valid_detections = [best_detection]

        # Draw detections (all in blue)
        for detection in valid_detections:
            bbox = detection["bbox"]
            x0 = bbox[0] * img.width
            y0 = bbox[1] * img.height
            x1 = (bbox[0] + bbox[2]) * img.width
            y1 = (bbox[1] + bbox[3]) * img.height

            rgb = ImageColor.getrgb("blue")
            alpha = int(pred_score * 255)
            color = (*rgb[:3], alpha)

            text = f"{prediction_label}: {pred_score:.2f}"
            text_rel_xy = font.getbbox(text, anchor="lt")
            text_bg_xy = (
                x0,
                y0,
                x0 + text_rel_xy[2] + 2 * border_size,
                y0 + text_rel_xy[3] + 2 * border_size,
            )
            text_color = (255, 255, 255, alpha)

            draw.rectangle((x0, y0, x1, y1), outline=color, width=border_size)
            draw.rectangle(text_bg_xy, fill=color, width=border_size)
            draw.text(
                (x0 + border_size, y0 + border_size),
                text,
                fill=text_color,
                font=font,
                anchor="lt",
            )

        # Save the modified image
        save_folder = output_folder if bear_detected else not_bear_folder
        output_path = os.path.join(save_folder, os.path.basename(img_path))
        Image.alpha_composite(img, overlay).convert("RGB").save(output_path)
        print(f"Saved: {output_path}")
        # Remove the JSON file from the output folder
        if os.path.exists(json_path):
            os.remove(json_path)
            print(f"Removed: {json_path}")
            
if __name__ == "__main__":
    app.run(main)
