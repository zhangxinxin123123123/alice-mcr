param(
    [Parameter(Mandatory = $true)][string]$HdDirectory,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

function New-ReconstructedSheet {
    param(
        [string]$Prefix,
        [int[]]$RowHeights,
        [int]$LowResolutionBottomStart,
        [int]$LowResolutionTargetBottom,
        [string]$FullSheetName
    )
    $scale = 4
    $canvas = [System.Drawing.Bitmap]::new(1536 * $scale, $LowResolutionTargetBottom * $scale,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($canvas)
    try {
        $graphics.Clear([System.Drawing.Color]::White)
        $top = 0
        for ($row = 0; $row -lt 5; $row++) {
            for ($column = 0; $column -lt 8; $column++) {
                $number = $row * 8 + $column + 1
                $path = Join-Path $HdDirectory ("{0}_{1:d2}.png" -f $Prefix, $number)
                $part = [System.Drawing.Image]::FromFile($path)
                try {
                    $graphics.DrawImageUnscaled($part, $column * 768, $top)
                }
                finally {
                    $part.Dispose()
                }
            }
            $top += $RowHeights[$row]
        }

        # The exported HD chunks stop shortly before the artwork footer. Fill only that
        # narrow missing strip from the supplied full sheet, upscaled with high-quality
        # interpolation. The main faces and all large text remain sourced from HD chunks.
        if ($LowResolutionBottomStart -lt $LowResolutionTargetBottom) {
            $fullPath = Join-Path $HdDirectory $FullSheetName
            $full = [System.Drawing.Image]::FromFile($fullPath)
            try {
                $source = [System.Drawing.Rectangle]::new(
                    0, $LowResolutionBottomStart, 1536,
                    $LowResolutionTargetBottom - $LowResolutionBottomStart)
                $destination = [System.Drawing.Rectangle]::new(
                    0, $LowResolutionBottomStart * $scale, 1536 * $scale,
                    ($LowResolutionTargetBottom - $LowResolutionBottomStart) * $scale)
                $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                $graphics.DrawImage($full, $destination, $source, [System.Drawing.GraphicsUnit]::Pixel)
            }
            finally {
                $full.Dispose()
            }
        }
    }
    finally {
        $graphics.Dispose()
    }
    return $canvas
}

function Save-HdTile {
    param(
        [System.Drawing.Bitmap]$Source,
        [int]$Left,
        [int]$Top,
        [int]$Right,
        [int]$Bottom,
        [string]$Name,
        [int]$Padding = 16
    )
    $x = [Math]::Max(0, $Left + $Padding)
    $y = [Math]::Max(0, $Top + $Padding)
    $width = [Math]::Max(1, [Math]::Min($Source.Width, $Right - $Padding) - $x)
    $height = [Math]::Max(1, [Math]::Min($Source.Height, $Bottom - $Padding) - $y)
    $tile = $Source.Clone(
        [System.Drawing.Rectangle]::new($x, $y, $width, $height),
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    try {
        $jpeg = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() |
            Where-Object { $_.MimeType -eq 'image/jpeg' } | Select-Object -First 1
        $quality = [System.Drawing.Imaging.EncoderParameters]::new(1)
        $quality.Param[0] = [System.Drawing.Imaging.EncoderParameter]::new(
            [System.Drawing.Imaging.Encoder]::Quality, [long]95)
        try {
            $tile.Save((Join-Path $OutputDirectory ($Name + '.jpg')), $jpeg, $quality)
        }
        finally {
            $quality.Dispose()
        }
    }
    finally {
        $tile.Dispose()
    }
}

function Split-HdEqualRow {
    param(
        [System.Drawing.Bitmap]$Source,
        [int]$Top,
        [int]$Bottom,
        [string[]]$Names
    )
    for ($i = 0; $i -lt $Names.Count; $i++) {
        $left = [Math]::Round($Source.Width * $i / $Names.Count)
        $right = [Math]::Round($Source.Width * ($i + 1) / $Names.Count)
        Save-HdTile -Source $Source -Left $left -Top $Top -Right $right -Bottom $Bottom -Name $Names[$i]
    }
}

$everydayRows = @(
    @('daily_hello','daily_thanks','daily_hard_work','daily_cling','daily_waiting','daily_no_more','daily_hmph','daily_cry'),
    @('daily_are_you_there','daily_okay','daily_love_you','daily_sleepy','daily_goodnight','daily_morning','daily_question','daily_wow'),
    @('daily_miss_you','daily_secret','daily_cheers','daily_please','daily_sorry','daily_hug','daily_ok','daily_no'),
    @('daily_angry','daily_aggrieved','daily_confused','daily_hehe','daily_heartbeat','daily_like','daily_shy','daily_peek'),
    @('daily_working','daily_eating','daily_on_the_way','daily_bathing','daily_buy','daily_congrats','daily_tired','daily_forever')
)
$everydayY = @(0, 180, 361, 543, 725, 909) | ForEach-Object { $_ * 4 }
$everyday = New-ReconstructedSheet -Prefix 'alice_general' -RowHeights @(692,692,692,692,692) `
    -LowResolutionBottomStart 865 -LowResolutionTargetBottom 909 `
    -FullSheetName 'FULL_a_large_cute_anime_sticker_sheet_emoji_pack_illu.png'
try {
    for ($row = 0; $row -lt $everydayRows.Count; $row++) {
        Split-HdEqualRow -Source $everyday -Top $everydayY[$row] -Bottom $everydayY[$row + 1] -Names $everydayRows[$row]
    }
}
finally {
    $everyday.Dispose()
}

$booking = New-ReconstructedSheet -Prefix 'alice_booking' -RowHeights @(720,720,720,720,724) `
    -LowResolutionBottomStart 901 -LowResolutionTargetBottom 970 `
    -FullSheetName 'FULL_a_wide_tightly_packed_sticker_sheet_emoji_sheet.png'
try {
    Split-HdEqualRow -Source $booking -Top 0 -Bottom (199*4) -Names @(
        'book_hello','book_welcome','book_want_to_book','book_which_time','book_check_space','book_choose_date','book_available','book_reserved'
    )
    Split-HdEqualRow -Source $booking -Top (199*4) -Bottom (399*4) -Names @(
        'book_thanks','book_please','book_ok','book_no_problem','book_sorry','book_full','book_confirm_again','book_change_time','book_checking'
    )
    Split-HdEqualRow -Source $booking -Top (399*4) -Bottom (599*4) -Names @(
        'book_need_room','book_package','book_first_discount','book_choose_girl','book_all_right','book_same_day','book_late_notice','book_phone'
    )
    Split-HdEqualRow -Source $booking -Top (599*4) -Bottom (799*4) -Names @(
        'book_see_you','book_kiss','book_hard_work','book_more_questions','book_contact_anytime','book_leave_to_me','book_next_time','book_goodnight'
    )
    $lastNames = @('book_complete','book_fill_details','book_change_or_cancel','book_waiting_for_you','book_any_needs','book_peek')
    $lastCells = @(
        @(0,192), @(192,384), @(384,576), @(960,1152), @(1152,1344), @(1344,1536)
    )
    for ($i = 0; $i -lt $lastNames.Count; $i++) {
        Save-HdTile -Source $booking -Left ($lastCells[$i][0]*4) -Top (799*4) `
            -Right ($lastCells[$i][1]*4) -Bottom (970*4) -Name $lastNames[$i]
    }
}
finally {
    $booking.Dispose()
}

$files = Get-ChildItem -LiteralPath $OutputDirectory -Filter '*.jpg'
Write-Output "Replaced $($files.Count) sticker images with reconstructed HD assets ($([Math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)) MB)."
