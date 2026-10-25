function outputTable = Link_Volumes_12_10_2026(inputFile, outputFile, modelsFile)
% Estimate hourly traffic volumes from an Inqueries workbook.
% Usage: Link_Volumes_12_10_2026(inputFile, outputFile, modelsFile)

if nargin == 0
    [inputName, inputFolder] = uigetfile('*.xlsx', 'Select Inqueries Excel File');
    if isequal(inputName, 0)
        error('LinkVolumes:InputCancelled', 'No input workbook was selected.');
    end
    inputFile = fullfile(inputFolder, inputName);

    [outputName, outputFolder] = uiputfile('*.xlsx', 'Save Volumes Workbook');
    if isequal(outputName, 0)
        error('LinkVolumes:OutputCancelled', 'No output workbook was selected.');
    end
    outputFile = fullfile(outputFolder, outputName);
elseif nargin < 2
    error('LinkVolumes:MissingPath', ...
        'Provide both the input workbook path and output workbook path.');
end

if nargin < 3 || isempty(modelsFile)
    modelsFile = fullfile(fileparts(mfilename('fullpath')), 'Models.mat');
end

validateattributes(inputFile, {'char', 'string'}, {'scalartext', 'nonempty'});
validateattributes(outputFile, {'char', 'string'}, {'scalartext', 'nonempty'});
validateattributes(modelsFile, {'char', 'string'}, {'scalartext', 'nonempty'});
inputFile = char(inputFile);
outputFile = char(outputFile);
modelsFile = char(modelsFile);

if ~isfile(inputFile)
    error('LinkVolumes:InputNotFound', 'Input workbook not found: %s', inputFile);
end
if ~isfile(modelsFile)
    error('LinkVolumes:ModelsNotFound', 'Models file not found: %s', modelsFile);
end

modelData = load(modelsFile, 'Models');
if ~isfield(modelData, 'Models')
    error('LinkVolumes:MissingModels', 'The models file must contain a variable named Models.');
end
Models = modelData.Models;
if ~isnumeric(Models) || ~isreal(Models) || size(Models, 2) < 10 || ...
        size(Models, 1) < 3 || any(~isfinite(Models(:)))
    error('LinkVolumes:InvalidModels', ...
        'Models must be a finite numeric matrix with at least 3 rows and 10 columns.');
end
if any(Models(:, 5) <= 0)
    error('LinkVolumes:InvalidModels', 'Every model must have a positive jam density.');
end

inputTable = readtable(inputFile, 'Sheet', 'Inqueries', ...
    'VariableNamingRule', 'preserve');
requiredColumns = {'R', 'Lanes', 'FFS', 'Actual Speed'};
missingColumns = requiredColumns(~ismember(requiredColumns, inputTable.Properties.VariableNames));
if ~isempty(missingColumns)
    error('LinkVolumes:MissingColumns', ...
        'The Inqueries sheet is missing required columns: %s', strjoin(missingColumns, ', '));
end
if height(inputTable) == 0
    error('LinkVolumes:EmptyInput', 'The Inqueries sheet contains no data rows.');
end

for columnIndex = 1:numel(requiredColumns)
    column = inputTable.(requiredColumns{columnIndex});
    if ~isnumeric(column) || ~isreal(column) || any(~isfinite(column(:)))
        error('LinkVolumes:InvalidInput', ...
            'Column "%s" must contain only finite numeric values.', requiredColumns{columnIndex});
    end
end

lanesInput = inputTable.('Lanes');
ffsInput = inputTable.('FFS');
speedInput = inputTable.('Actual Speed');
rowCount = height(inputTable);
volume = zeros(rowCount, 1);
tolerance = zeros(rowCount, 1);
nearestModelCount = 3;

for i = 1:rowCount
    lanes = lanesInput(i);
    ffs = ffsInput(i);
    speed = speedInput(i);

    modelLanesDifference = abs(Models(:, 2) - lanes);
    modelFfsDifference = abs(Models(:, 4) - ffs);
    modelDistances = [(1:size(Models, 1))', modelLanesDifference, modelFfsDifference];
    modelDistances = sortrows(modelDistances, [2 3]);
    nearestModelRows = modelDistances(1:nearestModelCount, 1);

    solutions = zeros(nearestModelCount, 2);
    for modelIndex = 1:nearestModelCount
        modelRow = nearestModelRows(modelIndex);
        modelFfs = Models(modelRow, 4);
        jamDensity = Models(modelRow, 5);
        a = Models(modelRow, 6);
        e = Models(modelRow, 7);
        b = Models(modelRow, 8);
        rmse = Models(modelRow, 10);

        density = 0:0.01:jamDensity;
        estimatedSpeed = modelFfs * (1 - (density / jamDensity) .^ a) ./ ...
            (1 + e .* (density / jamDensity) .^ b);

        speedDifference = abs(estimatedSpeed - speed);
        closestSpeedIndex = find(speedDifference == min(speedDifference), 1, 'first');
        estimatedDensity = density(closestSpeedIndex);
        solutions(modelIndex, :) = [estimatedDensity * speed, rmse * speed];
    end
    volume(i) = mean(solutions(:, 1));
    tolerance(i) = mean(solutions(:, 2));
end

if numel(inputTable.Properties.VariableNames) >= 5 && ...
        strcmp(inputTable.Properties.VariableNames{5}, 'Hour')
    hourInput = inputTable{:, 5};
else
    hourInput = nan(rowCount, 1);
end

outputTable = table(inputTable.('R'), inputTable.('Lanes'), ...
    inputTable.('FFS'), inputTable.('Actual Speed'), hourInput, volume, tolerance, ...
    'VariableNames', {'R', 'Lanes', 'FFS', 'Actual Speed', 'Hour', ...
    'Estimated Volume', 'RMSE Tolerance'});
writetable(outputTable, outputFile, 'Sheet', 'Volumes', 'WriteMode', 'overwritesheet');
end
