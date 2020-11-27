"""Base quality score recalibration."""
import os
import shutil

from resolwe.process import (
    Cmd,
    DataField,
    FileField,
    ListField,
    Process,
    SchedulingClass,
    StringField,
)


class BQSR(Process):
    """A two pass process of BaseRecalibrator and ApplyBQSR from GATK.

    See GATK website for more information on BaseRecalibrator.

    It is possible to modify read group using GATK's AddOrReplaceGroups through Replace read groups in BAM
    (``read_group``) input field.
    """

    slug = "bqsr"
    name = "BaseQualityScoreRecalibrator"
    process_type = "data:alignment:bam:bqsr:"
    version = "2.0.0"
    category = "BAM processing"
    scheduling_class = SchedulingClass.BATCH
    entity = {"type": "sample"}
    requirements = {
        "expression-engine": "jinja",
        "executor": {"docker": {"image": "resolwebio/dnaseq:5.2.0"}},
    }
    data_name = '{{ bam|sample_name|default("?") }}'

    class Input:
        """Input fields to perform Base quality score recalibration."""

        bam = DataField("alignment:bam", label="BAM file containing reads")
        reference = DataField("seq:nucleotide", label="Reference genome file")
        known_sites = ListField(
            DataField(
                data_type="variants:vcf",
                description="One or more databases of known polymorphic sites used to exclude regions around known "
                "polymorphisms from analysis.",
            ),
            label="List of known sites of variation",
        )
        intervals = DataField(
            data_type="bed",
            required=False,
            label="One or more genomic intervals over which to operate.",
            description="This field is optional, but it can speed up the process by restricting calculations to "
            "specific genome regions.",
        )
        read_group = StringField(
            label="Replace read groups in BAM",
            description="Replace read groups in a BAM file.This argument enables the user to replace all read groups "
            "in the INPUT file with a single new read group and assign all reads to this read group in "
            "the OUTPUT BAM file. Addition or replacement is performed using Picard's "
            "AddOrReplaceReadGroups tool. Input should take the form of -name=value delimited by a "
            '";", e.g. "-ID=1;-LB=GENIALIS;-PL=ILLUMINA;-PU=BARCODE;-SM=SAMPLENAME1". See tool\'s '
            "documentation for more information on tag names. Note that PL, LB, PU and SM are require "
            "fields. See caveats of rewriting read groups in the documentation.",
            default="",
        )
        validation_stringency = StringField(
            label="Validation stringency",
            description="Validation stringency for all SAM files read by this program. Setting stringency to SILENT "
            "can improve performance when processing a BAM file in which variable-length data (read, "
            "qualities, tags) do not otherwise need to be decoded. Default is STRICT. This setting is "
            "used in BaseRecalibrator and ApplyBQSR processes.",
            choices=[
                ("STRICT", "STRICT"),
                ("LENIENT", "LENIENT"),
                ("SILENT", "SILENT"),
            ],
            default="STRICT",
        )

    class Output:
        """Output fields to BaseQualityScoreRecalibrator."""

        bam = FileField(label="Base quality score recalibrated BAM file")
        bai = FileField(label="Index of base quality score recalibrated BAM file")
        stats = FileField(label="Alignment statistics")
        bigwig = FileField(label="BigWig file", required=False)
        species = StringField(label="Species")
        build = StringField(label="Build")
        recal_table = FileField(label="Recalibration tabled")

    def run(self, inputs, outputs):
        """Run the analysis."""
        # Prepare output file names.
        bam = os.path.basename(inputs.bam.bam.path)
        file_name = os.path.splitext(os.path.basename(inputs.bam.bam.path))[0]
        bam_rg = f"{file_name}_RG.bam"

        species = inputs.bam.species

        # Parse read_group argument from a string, delimited by a ; and =
        # into a form that will be accepted by AddOrReplaceReadGroups tool.
        # E.g. '-LB=DAB;-PL=Illumina;-PU=barcode;-SM=sample1' should become
        # ['-LB', 'DAB', '-PL', 'Illumina', '-PU', 'barcode', '-SM', 'sample1']
        # prepended by INPUT and OUTPUT.
        if inputs.read_group:
            arrg = [
                "--INPUT",
                f"{inputs.bam.bam.path}",
                "--VALIDATION_STRINGENCY",
                f"{inputs.validation_stringency}",
                "--OUTPUT",
                f"{bam_rg}",
            ]

            present_tags = []
            for x in inputs.read_group.split(";"):
                split_tag = x.split("=")
                arrg.extend(split_tag)
                present_tags.append(split_tag[0])

            # Make sure all arguments to read_group are valid.
            all_tags = {
                "-LB",
                "-PL",
                "-PU",
                "-SM",
                "-CN",
                "-DS",
                "-DT",
                "-FO",
                "-ID",
                "-KS",
                "-PG",
                "-PI",
                "-PM",
                "-SO",
            }
            present_tag_set = set(present_tags)
            check_all_tags = present_tag_set.issubset(all_tags)
            if not check_all_tags:
                self.error("One or more read_group argument(s) improperly formatted.")

            # Check that there are no double entries of arguments to read_group.
            if len(present_tag_set) != len(present_tags):
                self.error("You have duplicate tags in read_group argument.")

            # Check that all mandatory arguments to read_group are present.
            mandatory_tags = {"-LB", "-PL", "-PU", "-SM"}
            check_tags = mandatory_tags.issubset(present_tag_set)
            if not check_tags:
                self.error(
                    "Missing mandatory read_group argument(s) (-PL, -LB, -PU and -SM are mandatory)."
                )

            Cmd["gatk"]["AddOrReplaceReadGroups"](arrg)
        else:
            shutil.copy2(inputs.bam.bam.path, bam_rg)

        # Make sure the file is indexed.
        Cmd["samtools"]["index"](bam_rg)

        recal_table = f"{file_name}_recalibration.table"
        br_inputs = [
            "--input",
            f"{bam_rg}",
            "--output",
            f"{recal_table}",
            "--reference",
            f"{inputs.reference.fasta.path}",
            "--read-validation-stringency",
            f"{inputs.validation_stringency}",
        ]
        if inputs.intervals:
            br_inputs.extend(["--intervals", f"{inputs.intervals.bed.path}"])

        # Add known sites to the input parameters of BaseRecalibrator.
        for site in inputs.known_sites:
            br_inputs.extend(["--known-sites", f"{site.vcf.path}"])

        # Prepare bqsr recalibration file.
        Cmd["gatk"]["BaseRecalibrator"](br_inputs)

        self.progress(0.5)

        # Apply base recalibration.
        ab_inputs = [
            "--input",
            f"{bam_rg}",
            "--output",
            f"{bam}",
            "--reference",
            f"{inputs.reference.fasta.path}",
            "--bqsr-recal-file",
            f"{recal_table}",
            "--read-validation-stringency",
            f"{inputs.validation_stringency}",
        ]
        Cmd["gatk"]["ApplyBQSR"](ab_inputs)

        stats = f"{bam}_stats.txt"
        (Cmd["samtools"]["flagstat"][f"{bam}"] > stats)()

        self.progress(0.8)

        btb_inputs = [f"{bam}", f"{species}", f"{self.requirements.resources.cores}"]

        Cmd["bamtobigwig.sh"](btb_inputs)

        bigwig = file_name + ".bw"
        if not os.path.exists(bigwig):
            self.info("BigWig file not calculated.")
        else:
            outputs.bigwig = bigwig

        self.progress(0.9)

        outputs.bam = bam
        outputs.bai = file_name + ".bai"
        outputs.stats = stats

        outputs.species = species
        outputs.build = inputs.bam.build
        outputs.recal_table = recal_table
